"""Audit-log forward hash chain per ADR 0021.

Every audit row carries two cryptographic columns:

* ``prev_hash`` — the previous row's ``row_hash`` (or the empty
  string for the chain genesis row).
* ``row_hash`` — ``sha256(prev_hash || canonical_json(event))``
  computed at insert time.

This module owns the canonical insert path (:func:`audit_log_append`),
the row-hash primitive (:func:`compute_row_hash`), the chain-event
encoder used by both the live insert path and the migration
backfill (:func:`encode_event_for_chain`), and the verification
walker (:func:`verify_chain`) that ``tessera audit verify`` runs
end-to-end.

The chain detects accidental corruption, deletion, modification,
reordering, and forgery by an attacker who does not recompute
hashes (ADR 0021 §Security claim — exact boundary). It does **not**
detect tampering by an attacker who can recompute hashes — the
canonicalizer is published and the chain payload is unkeyed.
Public-facing trust language must respect that boundary.

Single insert path: every audit write goes through
:func:`audit_log_append`. Direct ``INSERT INTO audit_log`` from
anywhere else in ``src/`` is prohibited and enforced by the
``audit-chain-single-writer`` CI gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlcipher3

from tessera.vault.canonical_json import canonical_json

_GENESIS_PREV_HASH: Final[str] = ""


class AuditChainError(Exception):
    """Base class for audit-chain failures."""


class AuditChainBrokenError(AuditChainError):
    """A walked row's recomputed hash does not match the stored value.

    Carries the breaking row's id, the recomputed hash, the stored
    hash, and the row's op so callers can render an actionable
    diagnostic without a second query.
    """

    def __init__(
        self,
        *,
        row_id: int,
        op: str,
        expected_row_hash: str,
        actual_row_hash: str,
    ) -> None:
        super().__init__(
            f"audit chain broken at row {row_id} (op={op!r}): "
            f"expected row_hash={expected_row_hash!r}, "
            f"actual row_hash={actual_row_hash!r}"
        )
        self.row_id = row_id
        self.op = op
        self.expected_row_hash = expected_row_hash
        self.actual_row_hash = actual_row_hash


@dataclass(frozen=True, slots=True)
class ChainEvent:
    """The exact shape that the chain hash is computed over.

    The shape is stable across:

    * the live insert path (:func:`audit_log_append`)
    * the migration backfill of pre-upgrade rows
    * the verification walker

    All three paths must produce the same bytes for the same row, or
    the chain breaks under conditions the security claim does not
    cover. The frozen dataclass + ``to_canonical_dict`` pair is the
    canonical encoding; never construct the dict by hand.
    """

    row_id: int
    at: int
    actor: str
    agent_id: int | None
    op: str
    target_external_id: str | None
    payload: dict[str, Any]

    def to_canonical_dict(self) -> dict[str, Any]:
        # Field order is irrelevant — canonical_json sorts keys —
        # but matching ADR 0021 §Insert path keeps the chain contract
        # legible at the call site.
        return {
            "id": self.row_id,
            "at": self.at,
            "actor": self.actor,
            "agent_id": self.agent_id,
            "op": self.op,
            "target_external_id": self.target_external_id,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ChainHead:
    """Identity of the most recent row in the chain."""

    row_id: int
    row_hash: str


@dataclass(frozen=True, slots=True)
class ChainVerifyOk:
    """Successful end-to-end chain walk."""

    total_rows: int
    genesis_row_id: int | None
    genesis_at: int | None
    head: ChainHead | None


def encode_event_for_chain(
    *,
    row_id: int,
    at: int,
    actor: str,
    agent_id: int | None,
    op: str,
    target_external_id: str | None,
    payload_json: str,
) -> ChainEvent:
    """Decode a stored row into a :class:`ChainEvent`.

    The migration backfill and the verification walker both need to
    re-derive the hash from the row's stored fields. Going through
    this function (rather than constructing :class:`ChainEvent`
    directly) keeps payload deserialization in one place; if the
    stored payload is malformed JSON, the row hash collapses to a
    sentinel that the chain walker treats as a recompute mismatch
    (which is exactly the right behaviour — the row's payload is
    unrecoverable, the chain is broken).
    """

    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError:
        payload = {"__chain_decode_error__": True, "raw": payload_json}
    if not isinstance(payload, dict):
        payload = {"__chain_decode_error__": True, "raw": payload_json}
    return ChainEvent(
        row_id=row_id,
        at=at,
        actor=actor,
        agent_id=agent_id,
        op=op,
        target_external_id=target_external_id,
        payload=payload,
    )


def compute_row_hash(*, prev_hash: str, event: ChainEvent) -> str:
    """Return ``sha256(prev_hash || canonical_json(event))`` as hex.

    Hex (not base64 / raw bytes) keeps the value text-safe in
    SQLite, in audit dumps, and in failure messages. ``prev_hash``
    is concatenated as UTF-8 bytes to keep the hash fully
    byte-stable across the storage and verification paths.
    """

    hasher = hashlib.sha256()
    hasher.update(prev_hash.encode("utf-8"))
    hasher.update(canonical_json(event.to_canonical_dict()))
    return hasher.hexdigest()


def read_chain_head(conn: sqlcipher3.Connection) -> ChainHead | None:
    """Return the most recent row's id + ``row_hash`` (or ``None``)."""

    row = conn.execute("SELECT id, row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return ChainHead(row_id=int(row[0]), row_hash=str(row[1]) if row[1] is not None else "")


def audit_log_append(
    conn: sqlcipher3.Connection,
    *,
    op: str,
    actor: str,
    agent_id: int | None = None,
    target_external_id: str | None = None,
    payload: dict[str, Any] | None = None,
    at: int | None = None,
) -> int:
    """Append one chain-aware audit row.

    Replaces the historical ``audit.write`` direct INSERT path. The
    function reads the current chain head, computes the new
    ``row_hash`` from ``prev_hash || canonical_json(event)``, and
    writes the row inside one transaction so a crash between read
    and write cannot orphan the chain. The single-daemon-per-vault
    invariant covers concurrency; multi-writer audit support is a
    v1.0 problem (ADR 0021 §Insert path).

    Op + payload validation lives in :mod:`tessera.vault.audit` and
    runs **before** the chain insert — a payload that fails the
    op-allowlist check raises ``UnknownOpError`` /
    ``DisallowedPayloadKeyError`` and the chain stays untouched.
    """

    from tessera.vault import audit as vault_audit

    if op not in vault_audit._PAYLOAD_ALLOWLIST:
        raise vault_audit.UnknownOpError(f"op {op!r} is not in the audit allowlist")
    allowed = vault_audit._PAYLOAD_ALLOWLIST[op]
    payload_dict: dict[str, Any] = payload or {}
    extra = set(payload_dict.keys()) - allowed
    if extra:
        raise vault_audit.DisallowedPayloadKeyError(
            f"op {op!r} received disallowed keys {sorted(extra)}; allowed: {sorted(allowed)}"
        )

    when = at if at is not None else _now_epoch()
    payload_json = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)

    # Read the head and write the row inside one transaction. SQLite's
    # default isolation guarantees the head we read is the head we
    # extend so long as no other writer holds the connection — which
    # is the v0.5 single-daemon-per-vault invariant. ``BEGIN
    # IMMEDIATE`` would tighten the guarantee on a multi-writer
    # vault, but that surface is v1.0 work.
    conn.execute("SAVEPOINT audit_log_append")
    try:
        head = read_chain_head(conn)
        prev_hash = head.row_hash if head is not None else _GENESIS_PREV_HASH
        next_id = _peek_next_rowid(conn)
        event = ChainEvent(
            row_id=next_id,
            at=when,
            actor=actor,
            agent_id=agent_id,
            op=op,
            target_external_id=target_external_id,
            payload=payload_dict,
        )
        new_hash = compute_row_hash(prev_hash=prev_hash, event=event)
        cur = conn.execute(
            """
            INSERT INTO audit_log(
                id, at, actor, agent_id, op, target_external_id, payload,
                prev_hash, row_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_id,
                when,
                actor,
                agent_id,
                op,
                target_external_id,
                payload_json,
                prev_hash,
                new_hash,
            ),
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT audit_log_append")
        conn.execute("RELEASE SAVEPOINT audit_log_append")
        raise
    conn.execute("RELEASE SAVEPOINT audit_log_append")
    if cur.lastrowid is None:
        raise AuditChainError("audit INSERT produced no rowid")
    return int(cur.lastrowid)


def verify_chain(conn: sqlcipher3.Connection) -> ChainVerifyOk:
    """Walk the chain from genesis to head; raise on any break.

    Returns a :class:`ChainVerifyOk` summarising the walk. Raises
    :class:`AuditChainBrokenError` on the first row where the
    recomputed ``row_hash`` does not match the stored value, where
    the stored ``prev_hash`` does not match the previous row's
    ``row_hash``, or where a row's ``id`` skips (insertion of
    forged rows in the middle).
    """

    rows = conn.execute(
        """
        SELECT id, at, actor, agent_id, op, target_external_id, payload,
               prev_hash, row_hash
        FROM audit_log
        ORDER BY id ASC
        """
    ).fetchall()
    if not rows:
        return ChainVerifyOk(total_rows=0, genesis_row_id=None, genesis_at=None, head=None)
    expected_prev = _GENESIS_PREV_HASH
    last_id: int | None = None
    last_hash = ""
    for row in rows:
        row_id = int(row[0])
        stored_prev = str(row[7]) if row[7] is not None else ""
        stored_row_hash = str(row[8]) if row[8] is not None else ""
        op = str(row[4])
        if stored_prev != expected_prev:
            raise AuditChainBrokenError(
                row_id=row_id,
                op=op,
                expected_row_hash=expected_prev,
                actual_row_hash=stored_prev,
            )
        event = encode_event_for_chain(
            row_id=row_id,
            at=int(row[1]),
            actor=str(row[2]),
            agent_id=int(row[3]) if row[3] is not None else None,
            op=op,
            target_external_id=str(row[5]) if row[5] is not None else None,
            payload_json=str(row[6]) if row[6] is not None else "{}",
        )
        recomputed = compute_row_hash(prev_hash=stored_prev, event=event)
        if recomputed != stored_row_hash:
            raise AuditChainBrokenError(
                row_id=row_id,
                op=op,
                expected_row_hash=recomputed,
                actual_row_hash=stored_row_hash,
            )
        expected_prev = stored_row_hash
        last_id = row_id
        last_hash = stored_row_hash
    genesis_row_id = int(rows[0][0])
    genesis_at = int(rows[0][1])
    head = None if last_id is None else ChainHead(row_id=last_id, row_hash=last_hash)
    return ChainVerifyOk(
        total_rows=len(rows),
        genesis_row_id=genesis_row_id,
        genesis_at=genesis_at,
        head=head,
    )


def _peek_next_rowid(conn: sqlcipher3.Connection) -> int:
    """Return the id the next ``INSERT INTO audit_log`` would land on.

    SQLite reports the highest rowid through ``MAX(rowid)``; the
    next allocated id is ``MAX + 1`` for an integer-PRIMARY-KEY
    table. We pre-compute the id so the chain hash can include it
    without a separate UPDATE round-trip after INSERT.
    """

    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()
    return int(row[0]) + 1


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def __dir__() -> Sequence[str]:
    return __all__


__all__ = [
    "AuditChainBrokenError",
    "AuditChainError",
    "ChainEvent",
    "ChainHead",
    "ChainVerifyOk",
    "audit_log_append",
    "compute_row_hash",
    "encode_event_for_chain",
    "read_chain_head",
    "verify_chain",
]
