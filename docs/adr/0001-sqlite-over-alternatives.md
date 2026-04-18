# ADR 0001 — SQLite over DuckDB, LMDB, Postgres

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

The vault is the persistent layer of every Tessera deployment. It holds identity facets, capability tokens, audit log, vector indexes, and full-text indexes. The choice of storage engine is the most load-bearing architectural decision in the system — every other component (retrieval pipeline, sync, migration, portability claim) is downstream of it.

Candidate engines evaluated:

| Engine | Single-file | ACID | FTS | Vector | Concurrency | Licensing |
|---|---|---|---|---|---|---|
| SQLite | Yes | Yes | FTS5 built-in | via sqlite-vec | Serialized writes, concurrent reads | Public domain |
| DuckDB | Yes | Yes | FTS extension | VSS extension | OLAP-optimized, not OLTP | MIT |
| LMDB | Yes | Yes | No | No | MVCC | OpenLDAP |
| Postgres + pgvector | No (requires daemon) | Yes | tsvector | pgvector | Excellent | PostgreSQL |

## Decision

**Use SQLite as the single storage engine.** Writes via WAL journal mode. Full-text via FTS5. Vector via `sqlite-vec` virtual tables (see ADR 0002).

## Rationale

1. **Portability is the ideology.** Single-file SQLite means `cp vault.db ~/anywhere/` works. DuckDB shares this. LMDB, Postgres do not.
2. **Zero external services.** No daemon, no network port, no version-mismatch between library and server. `pip install tessera` cannot install Postgres.
3. **Reader/writer model fits the workload.** Tessera is write-light (one agent capturing per session) and read-heavy (retrieval on every recall). SQLite's serialized-writer/concurrent-reader model is correct. DuckDB's OLAP model is wrong for the high-selectivity point queries retrieval runs.
4. **FTS5 is first-party.** No separate index service to synchronize. LMDB has no FTS — we would have to layer Tantivy or Whoosh, breaking the single-file claim.
5. **Mature ecosystem.** Every language has a mature binding. Every engineer can open the vault with `sqlite3` at a shell. Forensic and debugging friction is near-zero.

## Consequences

**Positive:**
- Vault portability claim is true without qualification.
- `tessera doctor` can inspect any layer with standard `sqlite3` CLI.
- Single-writer semantics trivialize concurrency reasoning.

**Negative:**
- Write throughput ceiling is real. At sustained >100 writes/sec, WAL checkpointing becomes a bottleneck. Mitigated by batching and the low write-rate of the actual workload. Documented as a known limit.
- No built-in encryption at rest. Requires sqlcipher build or application-layer encryption (tracked separately; see threat model).
- Large vaults (>1M facets) may require sharding in future versions. Explicitly deferred.

## Alternatives considered

- **DuckDB**: Strong on analytics, weak on OLTP. Vector support via VSS extension is newer than sqlite-vec and less proven. Would require separate FTS layer. Net: comparable portability, worse fit.
- **LMDB**: Excellent concurrency and latency. No FTS, no vector. Would require 3 separate indexes bolted on. Net: breaks the single-file invariant.
- **Postgres + pgvector**: Best vector and FTS story, but requires a daemon. Kills the `pip install tessera` story. Net: correct for a hosted service, wrong for local-first.

## Revisit triggers

- Sustained write throughput needs exceed 500/sec.
- Vault size exceeds 10 GB in real deployments.
- sqlite-vec project becomes unmaintained (see ADR 0002).
