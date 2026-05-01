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
| 0004 | [Seven-facet identity model](0004-seven-facet-identity-model.md) | Superseded by 0010 |
| 0005 | [MCP as primary transport](0005-mcp-as-primary-transport.md) | Accepted |
| 0006 | [Ollama as default model runtime](0006-ollama-as-default-model-runtime.md) | Accepted |
| 0007 | [Token lifecycle: short TTL, refresh, Unix-socket default](0007-token-lifecycle.md) | Accepted |
| 0008 | [Adapter framework scope and registration](0008-adapter-framework-scope.md) | Accepted |
| 0009 | [SWCR opt-in pending ablation](0009-swcr-opt-in-pending-ablation.md) | Superseded by 0011 |
| 0010 | [Five-facet user-context model](0010-five-facet-user-context-model.md) | Accepted |
| 0011 | [SWCR default-on as cross-facet coherence primitive](0011-swcr-default-on-cross-facet-coherence.md) | Accepted |
| 0012 | [v0.3 People + Skills design](0012-v0-3-people-and-skills-design.md) | Accepted |
| 0013 | [REST surface alongside MCP](0013-rest-surface-alongside-mcp.md) | Accepted |
| 0014 | [ONNX-only model stack via fastembed](0014-onnx-only-stack.md) | Accepted |
| 0015 | [Graph backing for person/skill coherence](0015-graph-backing-for-person-skill-coherence.md) | Accepted |
| 0016 | [Memory volatility model](0016-memory-volatility-model.md) | Accepted |
| 0017 | [Agent profile as a first-class facet](0017-agent-profile-facet.md) | Accepted |
| 0018 | [Verification + retrospective facets](0018-verification-retrospective-facets.md) | Accepted |

## When to write an ADR

- A decision has trade-offs that are not self-evident from the code.
- A decision closes off an alternative that a reasonable reader would pick.
- A decision will be relitigated without a written record.
- A decision cuts across multiple modules.

## When NOT to write an ADR

- A decision is local to one file and reversible.
- A decision is forced by an external constraint (license, platform).
- A decision is a naming choice with no semantic consequence.
