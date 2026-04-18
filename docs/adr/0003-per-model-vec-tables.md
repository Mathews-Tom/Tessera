# ADR 0003 — Per-model vec tables over unified embedding space

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

Embedding models change. Users switch from `nomic-embed-text` (384 dim) to `voyage-3` (1024 dim) for quality, or to `all-MiniLM-L6-v2` (384 dim) for speed. Dimensions differ between models; cosine scores are not comparable across models even at identical dimensions.

Two storage strategies:

**Unified table.** Single `vec_facets` table with one active model. Switching embedders requires destructive re-embedding before any query against new space works.

**Per-model tables.** One `vec_<model_id>` virtual table per registered embedding model. Facets can hold embeddings in multiple spaces simultaneously. Query routes to the active table.

## Decision

**Per-model vec tables.** The `embedding_models` registry tracks registered models; each gets its own `vec_<id>` virtual table created at registration time. Exactly one model is flagged `is_active=1` at any time; query path routes to that table.

## Rationale

1. **Substrate independence requires embedder independence.** Tessera's core claim is that the agent survives substrate changes. An embedder is part of the substrate. Forcing destructive re-embedding on swap contradicts the claim.
2. **sqlite-vec dim constraint forces the issue.** Virtual-table dim is fixed at creation (ADR 0002). A unified table with varying dim is not expressible in sqlite-vec. The alternative would be to drop-and-recreate the table on every embedder swap, destroying all prior embeddings during the swap window.
3. **Graceful migration.** With per-model tables, the old space stays queryable while the new space is being populated. This enables the shadow-query consistency model documented in `system-design.md §Retrieval addendum`.
4. **Ablation and rollback.** A user can evaluate a new embedder against the old on the same vault by flipping `is_active`. Rollback from a bad embedder choice is one SQL statement, not a re-embed.

## Consequences

**Positive:**
- Embedder swap is non-destructive. Old vectors remain queryable until explicitly pruned via `tessera vault prune-old-models`.
- Shadow-query mode during re-embed is implementable (see `system-design.md §Retrieval addendum`).
- A/B comparison of embedders is possible within one vault.

**Negative:**
- Storage cost is N × (rows × dim × 4 bytes) where N is the number of registered models. For a 100K-facet vault with two registered 1024-dim models: ~800 MB just for vectors. Explicit prune operation required.
- Query routing logic must track `is_active` correctly. Wrong routing → silently degraded retrieval.
- Schema complexity: dynamic virtual-table creation per model registration, not a static `CREATE` at init.

## Schema shape

```sql
CREATE TABLE embedding_models (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,       -- 'ollama/nomic-embed-text'
  dim         INTEGER NOT NULL,
  added_at    INTEGER NOT NULL,
  is_active   INTEGER NOT NULL DEFAULT 0  -- exactly one row sets this
);

-- CHECK constraint: exactly one active model at a time.
CREATE UNIQUE INDEX embedding_models_one_active
  ON embedding_models(is_active) WHERE is_active = 1;

-- Per-model virtual table, created at model registration time:
--   CREATE VIRTUAL TABLE vec_<id> USING vec0(
--     facet_id INTEGER PRIMARY KEY,
--     embedding FLOAT[<dim>]
--   );
```

## Alternatives considered

- **Unified table with variable dim**: Not expressible in sqlite-vec.
- **One table per dim class (384, 768, 1024)**: Partial solution. Still destroys prior embeddings on same-dim model swap (e.g., `nomic-embed` → `bge-small`, both 384-dim). Rejected.
- **External vector store per model**: Contradicts ADR 0001 and ADR 0002.

## Revisit triggers

- Storage overhead of multiple registered models becomes a user-visible problem (expected: users keep 1–2 models active).
- sqlite-vec adds support for variable-dim tables.
- A consistent cross-model score normalization technique emerges (currently: scores are not comparable across spaces; retrieval is routed to one space per query).
