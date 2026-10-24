"""Persistent registration and safe renewal for file-based MCP connectors."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import sqlcipher3

from tessera.auth import tokens
from tessera.auth.tokens import VerifiedCapability
from tessera.connectors import get_connector
from tessera.connectors.base import McpServerSpec
from tessera.connectors.file_safety import UnsupportedConfigShapeError
from tessera.connectors.registry import file_based_clients
from tessera.observability.events import EventLevel, EventLog
from tessera.vault import audit

MANAGED_ACCESS_TTL_SECONDS: Final[int] = 90 * 24 * 60 * 60

_RotationTrigger = Literal["renewal", "owner_recovery"]


@dataclass(frozen=True, slots=True)
class ManagedInstallation:
    """One config file whose Tessera capability is daemon-managed."""

    id: int
    connector_id: str
    config_path: str
    agent_id: int
    active_capability_id: int
    pending_capability_id: int | None
    access_ttl_seconds: int


class RecoveryRefused(ValueError):
    """An owner request cannot safely recover the selected installation."""


def _registered_installations(
    conn: sqlcipher3.Connection, *, connector_id: str, config_path: Path | None
) -> list[ManagedInstallation]:
    path = str(config_path.resolve()) if config_path is not None else None
    rows = conn.execute(
        "SELECT id, connector_id, config_path, agent_id, active_capability_id, "
        "pending_capability_id, access_ttl_seconds FROM managed_connector_installations "
        "WHERE connector_id = ? AND (? IS NULL OR config_path = ?) ORDER BY id",
        (connector_id, path, path),
    ).fetchall()
    return [_installation_from_row(row) for row in rows]


def _installation_state(
    conn: sqlcipher3.Connection, *, installation: ManagedInstallation, now_epoch: int
) -> tuple[VerifiedCapability | None, str]:
    capability, state = _inspect_configured_capability(
        conn,
        connector_id=installation.connector_id,
        config_path=Path(installation.config_path),
        now_epoch=now_epoch,
    )
    if installation.pending_capability_id is not None:
        return capability, "pending_renewal"
    if capability is not None and not _matches_installation(capability, installation):
        return capability, _mismatch_state(capability, installation)
    return capability, state


def connector_status(
    conn: sqlcipher3.Connection,
    *,
    connector_id: str,
    config_path: Path | None,
    now_epoch: int,
    horizon_seconds: int,
) -> list[dict[str, int | str | bool | None]]:
    """Return redacted, read-only diagnostics for the local vault owner."""
    installations = _registered_installations(
        conn, connector_id=connector_id, config_path=config_path
    )
    if not installations:
        return [
            {
                "connector_id": connector_id,
                "config_path": str(config_path.resolve()) if config_path is not None else None,
                "expires_at": None,
                "next_renewal_at": None,
                "pending_state": "none",
                "current_error": "not_registered",
                "recoverable": False,
            }
        ]
    result: list[dict[str, int | str | bool | None]] = []
    for installation in installations:
        _, state = _installation_state(conn, installation=installation, now_epoch=now_epoch)
        row = conn.execute(
            "SELECT expires_at FROM capabilities WHERE id = ?",
            (installation.active_capability_id,),
        ).fetchone()
        expires_at = int(row[0]) if row is not None else None
        entry: dict[str, int | str | bool | None] = {
            "installation_id": installation.id,
            "connector_id": installation.connector_id,
            "config_path": installation.config_path,
            "expires_at": expires_at,
            "next_renewal_at": expires_at - horizon_seconds if expires_at is not None else None,
            "pending_state": "staged" if installation.pending_capability_id is not None else "none",
            "current_error": None if state == "resolved" else state,
            "recoverable": state == "expired_token",
        }
        if state == "expired_token":
            entry["recovery_hint"] = (
                f"Run tessera connect {connector_id} --recover; "
                "use --path for a custom config and --socket for a custom daemon."
            )
        result.append(entry)
    return result


def recover_expired(
    conn: sqlcipher3.Connection,
    *,
    connector_id: str,
    config_path: Path,
    now_epoch: int,
    url: str,
    event_log: EventLog | None = None,
) -> None:
    """Explicit local-owner recovery; an expired bearer never grants authority.

    The registered config must still contain the exact expired active token.
    No pending state is reconciled here and no unregistered config is adopted.
    """
    installations = _registered_installations(
        conn, connector_id=connector_id, config_path=config_path
    )
    if len(installations) != 1:
        raise RecoveryRefused("not_registered")
    installation = installations[0]
    configured, state = _installation_state(conn, installation=installation, now_epoch=now_epoch)
    if state != "expired_token" or configured is None:
        raise RecoveryRefused("token_still_live" if state == "resolved" else state)
    if not _rotate(
        conn,
        installation=installation,
        configured=configured,
        now_epoch=now_epoch,
        url=url,
        event_log=event_log,
        trigger="owner_recovery",
    ):
        raise RecoveryRefused("config_write_failed")


def register(
    conn: sqlcipher3.Connection,
    *,
    connector_id: str,
    config_path: str,
    agent_id: int,
    capability_id: int,
    access_ttl_seconds: int,
    now_epoch: int,
    actor: str = "system",
) -> int:
    """Persist a config already written successfully by ``tessera connect``."""

    path = Path(config_path)
    if not path.is_absolute():
        raise ValueError("managed connector config_path must be absolute")
    resolved_path = str(path.resolve())
    capability = conn.execute(
        "SELECT agent_id, client_name, token_class FROM capabilities WHERE id = ?",
        (capability_id,),
    ).fetchone()
    if capability is None:
        raise ValueError(f"managed connector capability {capability_id} does not exist")
    if int(capability[0]) != agent_id:
        raise ValueError("managed connector capability agent does not match registration")
    if str(capability[1]) != connector_id:
        raise ValueError("managed connector capability client does not match registration")
    if str(capability[2]) != "service":
        raise ValueError("managed connector capability must use the service token class")
    if access_ttl_seconds != MANAGED_ACCESS_TTL_SECONDS:
        raise ValueError("managed connector capabilities must have a 90-day access TTL")
    conn.execute(
        """
        INSERT INTO managed_connector_installations(
            connector_id, config_path, agent_id, active_capability_id,
            pending_capability_id, access_ttl_seconds, created_at, updated_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT(config_path) DO UPDATE SET
            connector_id = excluded.connector_id,
            agent_id = excluded.agent_id,
            active_capability_id = excluded.active_capability_id,
            pending_capability_id = NULL,
            access_ttl_seconds = excluded.access_ttl_seconds,
            updated_at = excluded.updated_at
        """,
        (
            connector_id,
            resolved_path,
            agent_id,
            capability_id,
            access_ttl_seconds,
            now_epoch,
            now_epoch,
        ),
    )
    row = conn.execute(
        "SELECT id FROM managed_connector_installations WHERE config_path = ?",
        (resolved_path,),
    ).fetchone()
    if row is None:  # pragma: no cover — SQLite UPSERT invariant
        raise RuntimeError("managed connector registration was not persisted")
    installation_id = int(row[0])
    audit.write(
        conn,
        op="connector_installation_registered",
        actor=actor,
        agent_id=agent_id,
        payload={
            "installation_id": installation_id,
            "connector_id": connector_id,
            "config_path": resolved_path,
            "active_capability_id": capability_id,
            "access_ttl_seconds": access_ttl_seconds,
        },
        at=now_epoch,
    )
    return installation_id


def due(
    conn: sqlcipher3.Connection, *, now_epoch: int, horizon_seconds: int
) -> list[ManagedInstallation]:
    """Return managed installations whose active capability expires within the horizon."""

    rows = conn.execute(
        """
        SELECT i.id, i.connector_id, i.config_path, i.agent_id,
               i.active_capability_id, i.pending_capability_id, i.access_ttl_seconds
        FROM managed_connector_installations AS i
        JOIN capabilities AS c ON c.id = i.active_capability_id
        WHERE i.pending_capability_id IS NULL
          AND c.revoked_at IS NULL
          AND c.expires_at <= ?
        ORDER BY c.expires_at ASC, i.id ASC
        """,
        (now_epoch + horizon_seconds,),
    ).fetchall()
    return [_installation_from_row(row) for row in rows]


def adopt_default_installations(conn: sqlcipher3.Connection, *, now_epoch: int) -> int:
    """Adopt valid existing default-path service configurations exactly once."""

    adopted = 0
    for connector_id in file_based_clients():
        connector = get_connector(connector_id)
        try:
            path = connector.default_path()
        except Exception:
            continue
        if not path.exists():
            continue
        resolved_path = str(path.resolve())
        existing = conn.execute(
            "SELECT 1 FROM managed_connector_installations WHERE config_path = ?",
            (resolved_path,),
        ).fetchone()
        if existing is not None:
            continue
        configured, _ = _read_configured_capability(
            conn,
            connector_id=connector_id,
            config_path=path,
            now_epoch=now_epoch,
        )
        if configured is None:
            continue
        if configured.client_name != connector_id or configured.token_class != "service":
            continue
        register(
            conn,
            connector_id=connector_id,
            config_path=resolved_path,
            agent_id=configured.agent_id,
            capability_id=configured.token_id,
            access_ttl_seconds=MANAGED_ACCESS_TTL_SECONDS,
            now_epoch=now_epoch,
            actor="daemon",
        )
        adopted += 1
    return adopted


def renew_due(
    conn: sqlcipher3.Connection,
    *,
    now_epoch: int,
    horizon_seconds: int,
    url: str,
    event_log: EventLog | None = None,
) -> int:
    """Renew due installations without revoking clients' active credentials."""

    renewed = 0
    for installation in due(conn, now_epoch=now_epoch, horizon_seconds=horizon_seconds):
        configured, state = _installation_state(
            conn, installation=installation, now_epoch=now_epoch
        )
        if configured is None or state != "resolved":
            _record_drift(
                conn,
                installation=installation,
                state=state,
                now_epoch=now_epoch,
                event_log=event_log,
            )
            continue
        if _rotate(
            conn,
            installation=installation,
            configured=configured,
            now_epoch=now_epoch,
            url=url,
            event_log=event_log,
            trigger="renewal",
        ):
            renewed += 1
    return renewed


def _rotate(
    conn: sqlcipher3.Connection,
    *,
    installation: ManagedInstallation,
    configured: VerifiedCapability,
    now_epoch: int,
    url: str,
    event_log: EventLog | None,
    trigger: _RotationTrigger,
) -> bool:
    """Stage, write, then promote for both pre-expiry renewal and owner recovery."""
    issued = tokens.issue(
        conn,
        agent_id=configured.agent_id,
        client_name=configured.client_name,
        token_class=configured.token_class,
        scope=configured.scope,
        now_epoch=now_epoch,
        access_ttl_seconds=installation.access_ttl_seconds,
        actor="daemon",
    )
    conn.execute(
        "UPDATE managed_connector_installations "
        "SET pending_capability_id = ?, updated_at = ? WHERE id = ?",
        (issued.token_id, now_epoch, installation.id),
    )
    _write_stage_audit(
        conn, installation=installation, issued=issued, now_epoch=now_epoch, trigger=trigger
    )
    _emit(
        event_log,
        level="info",
        event="renewal_staged",
        attrs=_event_attrs(
            installation,
            pending_capability_id=issued.token_id,
            expires_at=issued.expires_at,
            trigger=trigger,
        ),
        now_epoch=now_epoch,
    )
    connector = get_connector(installation.connector_id)
    try:
        result = connector.apply(
            Path(installation.config_path), McpServerSpec(url=url, token=issued.raw_token)
        )
    except Exception as exc:
        tokens.revoke(
            conn,
            token_id=issued.token_id,
            now_epoch=now_epoch,
            reason="renewal_config_write_failed",
            actor="daemon",
        )
        conn.execute(
            "UPDATE managed_connector_installations "
            "SET pending_capability_id = NULL, updated_at = ? WHERE id = ?",
            (now_epoch, installation.id),
        )
        _write_failure_audit(
            conn,
            installation=installation,
            pending_capability_id=issued.token_id,
            failure_class=type(exc).__name__,
            now_epoch=now_epoch,
            trigger=trigger,
        )
        _emit(
            event_log,
            level="error",
            event="renewal_write_failed",
            attrs=_event_attrs(
                installation,
                pending_capability_id=issued.token_id,
                failure_class=type(exc).__name__,
                trigger=trigger,
            ),
            now_epoch=now_epoch,
        )
        print(
            "[tesserad] connector renewal config write failed: "
            f"connector={installation.connector_id} installation_id={installation.id} "
            f"config_path={installation.config_path} failure_class={type(exc).__name__}",
            file=sys.stderr,
        )
        return False
    conn.execute(
        "UPDATE managed_connector_installations "
        "SET active_capability_id = ?, pending_capability_id = NULL, updated_at = ? WHERE id = ?",
        (issued.token_id, now_epoch, installation.id),
    )
    audit.write(
        conn,
        op="connector_renewal_completed",
        actor="daemon",
        agent_id=installation.agent_id,
        payload={
            "installation_id": installation.id,
            "connector_id": installation.connector_id,
            "config_path": installation.config_path,
            "previous_capability_id": installation.active_capability_id,
            "active_capability_id": issued.token_id,
            "expires_at": issued.expires_at,
            "trigger": trigger,
            "backup_written": result.backup_path is not None,
        },
        at=now_epoch,
    )
    _emit(
        event_log,
        level="info",
        event="renewal_completed",
        attrs=_event_attrs(
            installation,
            active_capability_id=issued.token_id,
            expires_at=issued.expires_at,
            backup_written=result.backup_path is not None,
            trigger=trigger,
        ),
        now_epoch=now_epoch,
    )
    return True


def reconcile_pending(
    conn: sqlcipher3.Connection, *, now_epoch: int, event_log: EventLog | None = None
) -> int:
    """Resolve interrupted renewal state without minting or overwriting a config."""

    rows = conn.execute(
        """
        SELECT id, connector_id, config_path, agent_id,
               active_capability_id, pending_capability_id, access_ttl_seconds
        FROM managed_connector_installations
        WHERE pending_capability_id IS NOT NULL
        ORDER BY id ASC
        """
    ).fetchall()
    reconciled = 0
    for row in rows:
        installation = _installation_from_row(row)
        pending_id = installation.pending_capability_id
        if pending_id is None:  # pragma: no cover — query contract
            continue
        trigger = _pending_trigger(conn, installation=installation, pending_id=pending_id)
        configured, state = _inspect_configured_capability(
            conn,
            connector_id=installation.connector_id,
            config_path=Path(installation.config_path),
            now_epoch=now_epoch,
        )
        if (
            configured is not None
            and state == "resolved"
            and _matches_installation(configured, installation, capability_id=pending_id)
        ):
            conn.execute(
                "UPDATE managed_connector_installations "
                "SET active_capability_id = ?, pending_capability_id = NULL, updated_at = ? WHERE id = ?",
                (pending_id, now_epoch, installation.id),
            )
            _write_reconciliation_audit(
                conn,
                installation=installation,
                pending_capability_id=pending_id,
                state="promoted",
                now_epoch=now_epoch,
                trigger=trigger,
            )
            _emit(
                event_log,
                level="info",
                event="renewal_reconciled",
                attrs=_event_attrs(
                    installation,
                    pending_capability_id=pending_id,
                    state="promoted",
                    trigger=trigger,
                ),
                now_epoch=now_epoch,
            )
            reconciled += 1
            continue
        if (
            configured is not None
            and state in {"resolved", "expired_token"}
            and _matches_installation(configured, installation)
        ):
            tokens.revoke(
                conn,
                token_id=pending_id,
                now_epoch=now_epoch,
                reason="renewal_interrupted",
                actor="daemon",
            )
            conn.execute(
                "UPDATE managed_connector_installations "
                "SET pending_capability_id = NULL, updated_at = ? WHERE id = ?",
                (now_epoch, installation.id),
            )
            _write_reconciliation_audit(
                conn,
                installation=installation,
                pending_capability_id=pending_id,
                state="discarded",
                now_epoch=now_epoch,
                trigger=trigger,
            )
            _emit(
                event_log,
                level="info",
                event="renewal_reconciled",
                attrs=_event_attrs(
                    installation,
                    pending_capability_id=pending_id,
                    state="discarded",
                    trigger=trigger,
                ),
                now_epoch=now_epoch,
            )
            reconciled += 1
            continue
        _record_drift(
            conn,
            installation=installation,
            state="neither",
            now_epoch=now_epoch,
            event_log=event_log,
            trigger=trigger,
        )
    return reconciled


def _pending_trigger(
    conn: sqlcipher3.Connection, *, installation: ManagedInstallation, pending_id: int
) -> _RotationTrigger | None:
    """Read durable origin; pre-attribution rotations remain explicitly unknown."""
    row = conn.execute(
        "SELECT payload FROM audit_log WHERE op = 'connector_renewal_staged' "
        "AND json_extract(payload, '$.installation_id') = ? "
        "AND json_extract(payload, '$.pending_capability_id') = ? ORDER BY id DESC LIMIT 1",
        (installation.id, pending_id),
    ).fetchone()
    if row is None:
        return None
    trigger = json.loads(str(row[0])).get("trigger")
    if trigger is None:
        return None
    if trigger == "renewal":
        return "renewal"
    if trigger == "owner_recovery":
        return "owner_recovery"
    raise ValueError("invalid persisted connector rotation trigger")


def _installation_from_row(row: tuple[int | str | None, ...]) -> ManagedInstallation:
    return ManagedInstallation(
        id=_as_int(row[0]),
        connector_id=str(row[1]),
        config_path=str(row[2]),
        agent_id=_as_int(row[3]),
        active_capability_id=_as_int(row[4]),
        pending_capability_id=_as_int(row[5]) if row[5] is not None else None,
        access_ttl_seconds=_as_int(row[6]),
    )


def _as_int(value: int | str | None) -> int:
    if isinstance(value, int):
        return value
    raise TypeError("managed connector query returned a non-integer identifier")


def _inspect_configured_capability(
    conn: sqlcipher3.Connection,
    *,
    connector_id: str,
    config_path: Path,
    now_epoch: int,
) -> tuple[VerifiedCapability | None, str]:
    if not config_path.exists():
        return None, "missing_config"
    try:
        raw_token = get_connector(connector_id).read_token(config_path)
    except (UnsupportedConfigShapeError, UnicodeDecodeError):
        return None, "malformed_config"
    except Exception as exc:
        return None, f"config_read_{type(exc).__name__}"
    if raw_token is None:
        return None, "missing_entry"
    return tokens.inspect_for_owner(conn, raw_token=raw_token, now_epoch=now_epoch)


def _read_configured_capability(
    conn: sqlcipher3.Connection,
    *,
    connector_id: str,
    config_path: Path,
    now_epoch: int,
) -> tuple[VerifiedCapability | None, str]:
    capability, state = _inspect_configured_capability(
        conn, connector_id=connector_id, config_path=config_path, now_epoch=now_epoch
    )
    return (capability if state == "resolved" else None), state


def _mismatch_state(verified: VerifiedCapability, installation: ManagedInstallation) -> str:
    if verified.token_id != installation.active_capability_id:
        return "unexpected_capability"
    if verified.agent_id != installation.agent_id:
        return "unexpected_agent"
    if verified.client_name != installation.connector_id:
        return "unexpected_client"
    if verified.token_class != "service":
        return "unexpected_token_class"
    if installation.access_ttl_seconds != MANAGED_ACCESS_TTL_SECONDS:
        return "unexpected_access_ttl"
    return "unexpected_capability"


def _matches_installation(
    verified: VerifiedCapability,
    installation: ManagedInstallation,
    *,
    capability_id: int | None = None,
) -> bool:
    expected_id = installation.active_capability_id if capability_id is None else capability_id
    return (
        verified.token_id == expected_id
        and verified.agent_id == installation.agent_id
        and verified.client_name == installation.connector_id
        and verified.token_class == "service"
        and installation.access_ttl_seconds == MANAGED_ACCESS_TTL_SECONDS
    )


def _write_stage_audit(
    conn: sqlcipher3.Connection,
    *,
    installation: ManagedInstallation,
    issued: tokens.IssuedToken,
    now_epoch: int,
    trigger: _RotationTrigger,
) -> None:
    audit.write(
        conn,
        op="connector_renewal_staged",
        actor="daemon",
        agent_id=installation.agent_id,
        payload={
            "installation_id": installation.id,
            "connector_id": installation.connector_id,
            "config_path": installation.config_path,
            "active_capability_id": installation.active_capability_id,
            "pending_capability_id": issued.token_id,
            "expires_at": issued.expires_at,
            "trigger": trigger,
        },
        at=now_epoch,
    )


def _write_failure_audit(
    conn: sqlcipher3.Connection,
    *,
    installation: ManagedInstallation,
    pending_capability_id: int,
    failure_class: str,
    now_epoch: int,
    trigger: _RotationTrigger,
) -> None:
    audit.write(
        conn,
        op="connector_renewal_write_failed",
        actor="daemon",
        agent_id=installation.agent_id,
        payload={
            "installation_id": installation.id,
            "connector_id": installation.connector_id,
            "config_path": installation.config_path,
            "active_capability_id": installation.active_capability_id,
            "pending_capability_id": pending_capability_id,
            "failure_class": failure_class,
            "backup_written": False,
            "trigger": trigger,
        },
        at=now_epoch,
    )


def _write_reconciliation_audit(
    conn: sqlcipher3.Connection,
    *,
    installation: ManagedInstallation,
    pending_capability_id: int,
    state: str,
    now_epoch: int,
    trigger: _RotationTrigger | None,
) -> None:
    audit.write(
        conn,
        op="connector_renewal_reconciled",
        actor="daemon",
        agent_id=installation.agent_id,
        payload={
            "installation_id": installation.id,
            "connector_id": installation.connector_id,
            "config_path": installation.config_path,
            "active_capability_id": installation.active_capability_id,
            "pending_capability_id": pending_capability_id,
            "state": state,
            "trigger": trigger,
        },
        at=now_epoch,
    )


def _record_drift(
    conn: sqlcipher3.Connection,
    *,
    installation: ManagedInstallation,
    state: str,
    now_epoch: int,
    event_log: EventLog | None,
    trigger: _RotationTrigger | None = None,
) -> None:
    audit.write(
        conn,
        op="connector_config_drift",
        actor="daemon",
        agent_id=installation.agent_id,
        payload={
            "installation_id": installation.id,
            "connector_id": installation.connector_id,
            "config_path": installation.config_path,
            "active_capability_id": installation.active_capability_id,
            "pending_capability_id": installation.pending_capability_id,
            "state": state,
            "trigger": trigger,
        },
        at=now_epoch,
    )
    _emit(
        event_log,
        level="warn",
        event="config_drift",
        attrs=_event_attrs(installation, state=state, trigger=trigger),
        now_epoch=now_epoch,
    )
    if state == "expired_token":
        print(
            f"[tesserad] connector={installation.connector_id} expired_token: "
            f"run tessera connect {installation.connector_id} --recover "
            "(use --path for a custom config and --socket for a custom daemon)",
            file=sys.stderr,
        )


def _event_attrs(
    installation: ManagedInstallation, **additional: int | str | bool | None
) -> dict[str, int | str | bool | None]:
    return {
        "installation_id": installation.id,
        "connector_id": installation.connector_id,
        "config_path": installation.config_path,
        **additional,
    }


def _emit(
    event_log: EventLog | None,
    *,
    level: EventLevel,
    event: str,
    attrs: dict[str, int | str | bool | None],
    now_epoch: int,
) -> None:
    if event_log is None:
        return
    event_log.emit(
        level=level,
        category="connectors",
        event=event,
        attrs=attrs,
        at=now_epoch,
    )


__all__ = [
    "MANAGED_ACCESS_TTL_SECONDS",
    "ManagedInstallation",
    "RecoveryRefused",
    "adopt_default_installations",
    "connector_status",
    "due",
    "reconcile_pending",
    "recover_expired",
    "register",
    "renew_due",
]
