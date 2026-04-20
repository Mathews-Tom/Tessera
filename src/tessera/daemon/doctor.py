"""Health-check matrix for ``tessera doctor``.

Each check produces a :class:`DoctorResult` with status OK/WARN/ERROR
and a short remediation hint. ``run_all`` runs the whole matrix and
returns the results plus an aggregate verdict; the CLI renders them
as a human-readable table.

Checks are deliberately cheap — a developer who just ran ``tessera
init`` or ``tessera connect`` expects doctor to return in under a
second, not probe every adapter in sequence. Network-touching checks
carry short timeouts and mark WARN, not ERROR, on unreachability —
the user may be intentionally offline.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from enum import StrEnum

import httpx
import sqlcipher3

from tessera.adapters import models_registry
from tessera.daemon.config import DaemonConfig
from tessera.vault.connection import ensure_vec_loaded


class DoctorStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorResult:
    name: str
    status: DoctorStatus
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    results: tuple[DoctorResult, ...]

    @property
    def verdict(self) -> DoctorStatus:
        statuses = {r.status for r in self.results}
        if DoctorStatus.ERROR in statuses:
            return DoctorStatus.ERROR
        if DoctorStatus.WARN in statuses:
            return DoctorStatus.WARN
        return DoctorStatus.OK


async def run_all(
    config: DaemonConfig,
    *,
    conn: sqlcipher3.Connection | None = None,
    httpx_client: httpx.AsyncClient | None = None,
) -> DoctorReport:
    """Run every diagnostic; return a full report.

    ``conn`` is optional so the CLI can run doctor against a vault it
    has already unlocked; when omitted, vault-dependent checks
    downgrade to a "not-unlocked" WARN. ``httpx_client`` can be
    injected for tests to control Ollama reachability.
    """

    results: list[DoctorResult] = []
    results.append(_check_bind_address(config))
    results.append(_check_passphrase_env())
    results.append(await _check_ollama(config.ollama_host, client=httpx_client))
    if conn is None:
        results.append(
            DoctorResult(
                name="vault",
                status=DoctorStatus.WARN,
                detail="vault not unlocked; rerun doctor after --vault / --passphrase",
            )
        )
    else:
        results.append(_check_sqlite_vec(conn))
        results.append(_check_active_model(conn))
        results.append(_check_schema_match(conn))
        results.append(_check_token_expiry(conn))
    results.append(_check_keyring())
    return DoctorReport(results=tuple(results))


def _check_bind_address(config: DaemonConfig) -> DoctorResult:
    """ERROR when someone else already owns the daemon's HTTP port."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        result = sock.connect_ex((config.http_host, config.http_port))
    finally:
        sock.close()
    if result == 0:
        return DoctorResult(
            name="bind_address",
            status=DoctorStatus.ERROR,
            detail=(
                f"{config.http_host}:{config.http_port} is already in use; "
                "stop the other listener or pass --port"
            ),
        )
    return DoctorResult(
        name="bind_address",
        status=DoctorStatus.OK,
        detail=f"{config.http_host}:{config.http_port} is free",
    )


def _check_passphrase_env() -> DoctorResult:
    env_var = os.environ.get("TESSERA_PASSPHRASE_ENV", "TESSERA_PASSPHRASE")
    if os.environ.get(env_var):
        return DoctorResult(
            name="passphrase",
            status=DoctorStatus.OK,
            detail=f"{env_var} is set in the environment",
        )
    return DoctorResult(
        name="passphrase",
        status=DoctorStatus.WARN,
        detail=f"{env_var} not set; daemon will refuse to start without a passphrase",
    )


async def _check_ollama(host: str, *, client: httpx.AsyncClient | None = None) -> DoctorResult:
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=host, timeout=1.0)
    try:
        assert client is not None
        try:
            resp = await client.get("/api/tags")
        except (httpx.HTTPError, OSError) as exc:
            return DoctorResult(
                name="ollama",
                status=DoctorStatus.WARN,
                detail=f"ollama unreachable at {host}: {type(exc).__name__}",
            )
        if resp.status_code != 200:
            return DoctorResult(
                name="ollama",
                status=DoctorStatus.WARN,
                detail=f"ollama {host} returned HTTP {resp.status_code}",
            )
        return DoctorResult(
            name="ollama",
            status=DoctorStatus.OK,
            detail=f"ollama reachable at {host}",
        )
    finally:
        if own_client and client is not None:
            await client.aclose()


def _check_sqlite_vec(conn: sqlcipher3.Connection) -> DoctorResult:
    try:
        ensure_vec_loaded(conn)
        row = conn.execute("SELECT vec_version()").fetchone()
    except Exception as exc:
        return DoctorResult(
            name="sqlite_vec",
            status=DoctorStatus.ERROR,
            detail=f"sqlite-vec failed to load: {type(exc).__name__}: {exc}",
        )
    return DoctorResult(
        name="sqlite_vec",
        status=DoctorStatus.OK,
        detail=f"sqlite-vec loaded (version {row[0] if row else 'unknown'})",
    )


def _check_active_model(conn: sqlcipher3.Connection) -> DoctorResult:
    try:
        model = models_registry.active_model(conn)
    except models_registry.NoActiveModelError:
        return DoctorResult(
            name="active_model",
            status=DoctorStatus.ERROR,
            detail="no active embedding model; run `tessera models set --activate`",
        )
    return DoctorResult(
        name="active_model",
        status=DoctorStatus.OK,
        detail=f"active model: {model.name} (dim={model.dim})",
    )


def _check_schema_match(conn: sqlcipher3.Connection) -> DoctorResult:
    from tessera.vault.connection import BINARY_SCHEMA_VERSION

    row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
    if row is None:
        return DoctorResult(
            name="schema_match",
            status=DoctorStatus.ERROR,
            detail="vault has no schema_version; run `tessera init`",
        )
    schema = int(row[0])
    if schema == BINARY_SCHEMA_VERSION:
        return DoctorResult(
            name="schema_match",
            status=DoctorStatus.OK,
            detail=f"vault schema v{schema} matches binary",
        )
    if schema < BINARY_SCHEMA_VERSION:
        return DoctorResult(
            name="schema_match",
            status=DoctorStatus.WARN,
            detail=f"vault at v{schema}, binary supports v{BINARY_SCHEMA_VERSION}; migrate",
        )
    return DoctorResult(
        name="schema_match",
        status=DoctorStatus.ERROR,
        detail=f"vault at v{schema} newer than binary v{BINARY_SCHEMA_VERSION}; upgrade tessera",
    )


def _check_token_expiry(conn: sqlcipher3.Connection) -> DoctorResult:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM capabilities
         WHERE revoked_at IS NULL
        """
    ).fetchone()
    active = int(row[0]) if row else 0
    if active == 0:
        return DoctorResult(
            name="tokens",
            status=DoctorStatus.WARN,
            detail="no non-revoked capability tokens; run `tessera tokens create`",
        )
    return DoctorResult(
        name="tokens",
        status=DoctorStatus.OK,
        detail=f"{active} non-revoked capability token(s)",
    )


def _check_keyring() -> DoctorResult:
    try:
        import keyring

        backend = keyring.get_keyring().__class__.__name__
    except Exception as exc:
        return DoctorResult(
            name="keyring",
            status=DoctorStatus.WARN,
            detail=f"keyring unavailable ({type(exc).__name__}); env-var passphrase only",
        )
    return DoctorResult(
        name="keyring",
        status=DoctorStatus.OK,
        detail=f"keyring backend: {backend}",
    )


__all__ = [
    "DoctorReport",
    "DoctorResult",
    "DoctorStatus",
    "run_all",
]
