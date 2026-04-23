"""Append-only audit log with per-op payload allowlist.

The audit log is the legal-grade record of vault mutations, separate from
``events.db`` which carries operational telemetry. Per docs/threat-model.md
§S4 the allowlist is explicit and enforced on every write: payloads carry
IDs and operation metadata, never facet content, query text, token values,
or embedding vectors.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final

import sqlcipher3

OpName = str

# Ops emitted in the P1 scope. New ops from P2 onward extend this table in
# the same commit that introduces the emitter, keeping the allowlist closed.
_PAYLOAD_ALLOWLIST: Final[dict[OpName, frozenset[str]]] = {
    "vault_init": frozenset({"schema_version", "kdf_version", "vault_id"}),
    "vault_opened": frozenset({"schema_version"}),
    "vault_closed": frozenset({"duration_ms"}),
    "migration_started": frozenset({"from_version", "to_version", "backup_path"}),
    "migration_committed": frozenset({"from_version", "to_version", "duration_ms"}),
    "migration_interrupted": frozenset({"schema_target", "elapsed_seconds"}),
    "migration_resumed": frozenset({"schema_target"}),
    "migration_rolledback": frozenset({"from_version", "backup_path"}),
    "facet_inserted": frozenset(
        {"facet_type", "source_tool", "is_duplicate", "content_hash_prefix"}
    ),
    "facet_soft_deleted": frozenset({"facet_type"}),
    "facet_hard_deleted": frozenset({"facet_type"}),
    # ``forget`` is the MCP-surface soft-delete primitive post-reframe
    # (ADR 0010). The op carries the facet type and an optional free-text
    # reason the caller supplied; external_id lives on the audit row's
    # ``target_external_id`` column, not inside the payload.
    "forget": frozenset({"facet_type", "reason"}),
    # Retrieval pipeline (P4). The allowlist excludes query_text and
    # facet content — docs/determinism-and-observability.md §Audit log
    # fields for replay — so stolen-vault forensics cannot reconstruct
    # what the agent searched for from this table alone.
    "retrieval_executed": frozenset(
        {
            "seed",
            "retrieval_mode",
            "facet_types",
            "k",
            "duration_ms",
            "stage_ms",
            "candidate_counts",
            "result_count",
            "result_facet_ids",
            "rerank_degraded",
            "truncated",
            # ``type(exc).__name__: str(exc)`` when a stage raised; null on
            # clean completion. Type name + message only, not traceback —
            # tracebacks can carry local-variable content that would break
            # the §S4 no-content guarantee.
            "pipeline_error",
        }
    ),
    "retrieval_rerank_degraded": frozenset({"seed", "reranker_name", "reason"}),
    # Capability lifecycle (P7). ``token_id`` is the capabilities rowid and
    # ``token_hash_prefix`` is the first 12 chars of the stored sha256 —
    # enough for forensics to correlate issue → refresh → revoke without
    # disclosing the raw token. Raw token values never land in the audit
    # log (§S4 threat model).
    "token_issued": frozenset(
        {"token_id", "token_class", "client_name", "token_hash_prefix", "expires_at"}
    ),
    "token_refreshed": frozenset(
        {
            "old_token_id",
            "new_token_id",
            "token_class",
            "client_name",
            "token_hash_prefix",
            "expires_at",
        }
    ),
    "token_revoked": frozenset(
        {"token_id", "token_class", "client_name", "token_hash_prefix", "reason"}
    ),
    "auth_denied": frozenset({"client_name", "reason"}),
    "scope_denied": frozenset({"token_id", "client_name", "required_op", "required_facet_type"}),
}


class AuditError(Exception):
    """Base class for audit-log errors."""


class UnknownOpError(AuditError):
    """Op name is not in the allowlist."""


class DisallowedPayloadKeyError(AuditError):
    """Payload carries keys outside the op's allowlist."""


def allowed_ops() -> frozenset[str]:
    return frozenset(_PAYLOAD_ALLOWLIST.keys())


def allowed_keys(op: OpName) -> frozenset[str]:
    if op not in _PAYLOAD_ALLOWLIST:
        raise UnknownOpError(f"op {op!r} is not in the audit allowlist")
    return _PAYLOAD_ALLOWLIST[op]


def write(
    conn: sqlcipher3.Connection,
    *,
    op: OpName,
    actor: str,
    agent_id: int | None = None,
    target_external_id: str | None = None,
    payload: dict[str, Any] | None = None,
    at: int | None = None,
) -> int:
    """Append an audit row. Returns the inserted rowid.

    Raises :class:`UnknownOpError` when ``op`` is not in the allowlist and
    :class:`DisallowedPayloadKeyError` when ``payload`` carries keys outside
    the per-op allowlist. The allowlist is closed by design: adding a new op
    requires editing this module in the same commit as the emitter.
    """

    if op not in _PAYLOAD_ALLOWLIST:
        raise UnknownOpError(f"op {op!r} is not in the audit allowlist")
    allowed = _PAYLOAD_ALLOWLIST[op]
    payload = payload or {}
    extra = set(payload.keys()) - allowed
    if extra:
        raise DisallowedPayloadKeyError(
            f"op {op!r} received disallowed keys {sorted(extra)}; allowed: {sorted(allowed)}"
        )
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO audit_log(at, actor, agent_id, op, target_external_id, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            at if at is not None else _now_epoch(),
            actor,
            agent_id,
            op,
            target_external_id,
            payload_json,
        ),
    )
    if cur.lastrowid is None:
        raise AuditError("audit INSERT produced no rowid")
    return int(cur.lastrowid)


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
