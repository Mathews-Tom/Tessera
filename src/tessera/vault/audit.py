"""Append-only audit log with per-op payload allowlist.

The audit log is the legal-grade record of vault mutations, separate from
``events.db`` which carries operational telemetry. Per docs/threat-model.md
§S4 the allowlist is explicit and enforced on every write: payloads carry
IDs and operation metadata, never facet content, query text, token values,
or embedding vectors.

V0.5-P8 (ADR 0021) makes ``audit_log`` tamper-evident: every row carries a
``prev_hash`` / ``row_hash`` pair forming a forward-only linear hash chain
walked by ``tessera audit verify``. The chain insert path lives in
:mod:`tessera.vault.audit_chain`; this module's :func:`write` delegates to
it so the per-op payload allowlist and the chain-aware insert remain a
single function for callers (capture, retrieval, auth, daemon, migration).
"""

from __future__ import annotations

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
        {
            "facet_type",
            "source_tool",
            "is_duplicate",
            "content_hash_prefix",
            # ADR 0016: capture records lifecycle so forensics can tell a
            # session-scoped row from a persistent capture. ``ttl_seconds``
            # is an integer or null and never carries content.
            "volatility",
            "ttl_seconds",
        }
    ),
    "facet_soft_deleted": frozenset({"facet_type"}),
    "facet_hard_deleted": frozenset({"facet_type"}),
    # Auto-compaction sweep (V0.5-P1). The sweep soft-deletes expired
    # session/ephemeral rows. Forensics correlates via target_external_id;
    # the payload records why and at what age in seconds.
    "facet_auto_compacted": frozenset({"facet_type", "volatility", "age_seconds"}),
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
    # Daemon lifecycle: ``daemon_warmed`` records the result of the
    # supervisor's explicit embedder + reranker warm-up at startup. The
    # resolved reranker device string is the operator-visible signal for
    # which performance tier (cpu / mps / cuda) the daemon is running on.
    # No user content crosses the boundary: the warm-up uses the literal
    # string ``"warm"`` as input.
    "daemon_warmed": frozenset({"reranker_device", "embedder_name", "duration_ms"}),
    # People surface (v0.3). canonical_name and alias strings are
    # treated as user content per §S4 and never land in payloads —
    # forensics correlates rows via target_external_id and counts.
    # Cross-references to a *second* people row (merge / split) carry
    # the related external_id so the audit trail can reconstruct the
    # graph mutation.
    "person_created": frozenset({"alias_count"}),
    "person_alias_added": frozenset({"alias_count_after"}),
    "person_merged": frozenset({"secondary_external_id", "mentions_migrated", "aliases_migrated"}),
    "person_split": frozenset({"new_external_id"}),
    "person_mention_linked": frozenset({"person_external_id", "confidence"}),
    "person_mention_unlinked": frozenset({"person_external_id"}),
    # Skills surface (v0.3). Procedure markdown and metadata fields are
    # user content and never land in payloads — only the changed-field
    # name list and content-hash prefixes correlate forensics rows.
    "skill_procedure_updated": frozenset({"content_hash_prefix", "embed_status_reset"}),
    "skill_metadata_updated": frozenset({"fields_changed"}),
    "skill_disk_path_set": frozenset(set()),
    "skill_disk_path_cleared": frozenset(set()),
    # Agent profile link mutation (V0.5-P2 / ADR 0017). The facet itself
    # is captured through ``facet_inserted``; these ops correlate the
    # agents.profile_facet_external_id pointer's history. Payloads carry
    # only IDs and a structural prior-pointer reference — profile content
    # and structured metadata never land in audit rows (§S4 — no user
    # content, mirroring the people / skills surfaces).
    "agent_profile_link_set": frozenset({"prior_external_id"}),
    "agent_profile_link_cleared": frozenset(set()),
    # Compiled artifact registration (V0.5-P4 / ADR 0019). The
    # pair-write inserts the compiled_artifacts row and the
    # compiled_notebook facet under one external_id. The artifact
    # content itself rides the standard facets table; this op
    # carries the compile-side provenance only — artifact type,
    # compiler version, and the source-count cardinality. Source
    # ULIDs stay on ``compiled_artifacts.source_facets`` (a JSON
    # array column) rather than the audit row, so §S4 stays inside
    # the no-user-content contract.
    "compiled_artifact_registered": frozenset(
        {"artifact_type", "compiler_version", "source_count"}
    ),
    # Compiled-artifact staleness (V0.5-P6 / ADR 0019 §Rationale 6).
    # Emitted once per compiled_artifacts row that flips from
    # ``is_stale = 0`` to ``is_stale = 1`` because one of its source
    # facets mutated (capture, soft_delete, or skill procedure
    # update). ``source_external_id`` is the ULID of the mutating
    # source facet (an internal vault row identifier, not user
    # content); ``source_op`` records which mutation path emitted
    # the flip so forensics can reconstruct the cascade. Source
    # content, query text, and metadata never enter the payload —
    # §S4 boundary preserved.
    "compiled_artifact_marked_stale": frozenset({"source_external_id", "source_op"}),
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
    """Append an audit row through the V0.5-P8 chain insert path.

    Validation (op + payload allowlist) runs inside
    :func:`tessera.vault.audit_chain.audit_log_append`; this function
    is the historical entry point that capture / retrieval / auth /
    daemon / migration call. Routing every write through one
    chain-aware function is the ADR 0021 §Insert path single-writer
    invariant, enforced statically by the
    ``audit-chain-single-writer`` CI gate.

    Raises :class:`UnknownOpError` when ``op`` is not in the
    allowlist, :class:`DisallowedPayloadKeyError` when ``payload``
    carries keys outside the per-op allowlist, and
    :class:`AuditError` (or its
    :class:`tessera.vault.audit_chain.AuditChainError` subclass)
    when the chain insert fails.
    """

    from tessera.vault import audit_chain

    return audit_chain.audit_log_append(
        conn,
        op=op,
        actor=actor,
        agent_id=agent_id,
        target_external_id=target_external_id,
        payload=payload,
        at=at,
    )
