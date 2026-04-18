# ADR 0002 — sqlite-vec over pgvector, Qdrant, Chroma

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

Retrieval requires dense-vector similarity search over embedded facets. Candidates evaluated:

| Store | Deployment | Integrates with SQLite | Dim fixed at creation | License |
|---|---|---|---|---|
| sqlite-vec | Embedded in SQLite | Yes (same file) | Yes | Apache 2.0 |
| pgvector | Requires Postgres | No | No | PostgreSQL |
| Qdrant | Separate daemon | No | No | Apache 2.0 |
| Chroma | Separate process or embedded | No (separate files) | No | Apache 2.0 |
| FAISS | In-process library | No (separate file) | Yes | MIT |
| Milvus / Weaviate | Separate cluster | No | No | Various |

## Decision

**Use sqlite-vec as the sole vector index engine.** Vectors live in virtual tables inside the same `vault.db` file as relational and FTS5 data.

## Rationale

1. **Invariant alignment with ADR 0001.** The vault-is-a-single-file claim dies the moment a separate vector store is added. sqlite-vec is the only option that preserves it.
2. **Joint transactions.** Facet insertion, audit log entry, and embedding storage commit in one SQLite transaction. External stores require two-phase commit semantics we would have to build.
3. **No cross-process marshaling.** FAISS in-process works technically but forces a separate file plus a memory-mapped index build, reintroducing the migration and backup fragility single-file was chosen to avoid.
4. **Good-enough performance at target scale.** Target: <500 ms recall at 10K facets, <2 s at 100K. sqlite-vec benchmarks indicate linear scan is acceptable to 100K for 384–1024-dim vectors on M-series hardware; ANN index can be added later if needed.

## Consequences

**Positive:**
- Backup, restore, and portability are single-file operations.
- No daemon, no separate port, no version-coupling between vector store and relational store.
- Audit log records every vector write trivially.

**Negative:**
- sqlite-vec virtual tables fix dimensionality at creation. Forces ADR 0003 (per-model vec tables).
- No ANN index in early sqlite-vec versions; linear scan caps practical vault size at ~100K–500K facets. Documented as a scaling threshold.
- sqlite-vec is a younger project than pgvector or FAISS. Bus factor is real.

## Alternatives considered

- **pgvector**: Requires Postgres, fails ADR 0001. Off the table on first constraint.
- **Qdrant**: High-quality but introduces a second storage service. Deployment friction violates the "no Docker" posture.
- **FAISS in-process**: Technically single-machine but not single-file. Index is a separate file with its own corruption surface. Net: loses the single-file ideology to a technicality.
- **Chroma embedded**: Separate SQLite file plus parquet. Two files, not one. Rejected for same reason.

## Revisit triggers

- sqlite-vec development halts for more than 6 months.
- Real-world vaults consistently exceed 500K facets with recall latency > 2 s.
- A drop-in replacement with first-class SQLite integration and ANN support emerges.

## Mitigation if sqlite-vec is abandoned

The vector-index layer is an internal abstraction behind the retrieval pipeline. Switching engines requires a schema migration and a re-embed pass. The `embedding_models` registry (see system-design.md schema) already supports coexistence of multiple vector backends per facet; this is the migration path.
