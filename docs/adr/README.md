# Architecture Decision Records

**Status:** Draft 1
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

This directory captures the load-bearing architectural decisions behind Tessera. Each record follows the Nygard format: context, decision, consequences, alternatives considered.

ADRs are numbered sequentially. Numbers are never reused. Superseding decisions reference the prior ADR by number and status.

## Status values

| Status | Meaning |
|---|---|
| Proposed | Under review, not yet committed |
| Accepted | Committed; code written against it |
| Superseded | Replaced by a later ADR (cite ADR number in header) |
| Deprecated | No longer current, retained for history |

## Index

| # | Title | Status |
|---|---|---|
| 0001 | [SQLite over DuckDB, LMDB, Postgres](0001-sqlite-over-alternatives.md) | Accepted |
| 0002 | [sqlite-vec over pgvector, Qdrant, Chroma](0002-sqlite-vec-over-external-vector-stores.md) | Accepted |
| 0003 | [Per-model vec tables over unified embedding space](0003-per-model-vec-tables.md) | Accepted |
| 0004 | [Seven-facet identity model](0004-seven-facet-identity-model.md) | Accepted |
| 0005 | [MCP as primary transport](0005-mcp-as-primary-transport.md) | Accepted |
| 0006 | [Ollama as default model runtime](0006-ollama-as-default-model-runtime.md) | Accepted |
| 0007 | [Token lifecycle: short TTL, refresh, Unix-socket default](0007-token-lifecycle.md) | Accepted |
| 0008 | [Adapter framework scope and registration](0008-adapter-framework-scope.md) | Accepted |

## When to write an ADR

- A decision has trade-offs that are not self-evident from the code.
- A decision closes off an alternative that a reasonable reader would pick.
- A decision will be relitigated without a written record.
- A decision cuts across multiple modules.

## When NOT to write an ADR

- A decision is local to one file and reversible.
- A decision is forced by an external constraint (license, platform).
- A decision is a naming choice with no semantic consequence.
