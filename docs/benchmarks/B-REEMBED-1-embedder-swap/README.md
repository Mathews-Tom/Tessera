# B-REEMBED-1 — embedder-swap wall time

**Goal**: Record the wall-clock cost of rotating the active embedder at 10K facets — register a new embedding model, re-embed every existing facet against it, swap `is_active`, validate no gap.

**DoD target** (`docs/release-spec.md §v0.1 DoD`): 10K facets re-embedded in < 10 min on the reference hardware baseline.

## Reproduce

```bash
uv run python docs/benchmarks/B-REEMBED-1-embedder-swap/run.py --facets 10000
```

## Shape

1. Bootstrap an encrypted vault.
2. Register model A, capture N facets, embed them.
3. Register model B with a different dimension, which forces a fresh `vec_<id>` virtual table.
4. Activate model B.
5. Mark every existing facet `embed_status='pending'` (simulating the re-embed trigger).
6. Drain the embed worker against model B's adapter; measure wall clock.
7. Record p50/p95/p99 per-batch durations and the total re-embed time.

Fake adapters are used by default so the measurement isolates the
storage + worker costs from provider throughput. The DoD number lives
against a live Ollama baseline; this harness's purpose is to pin the
storage-side ceiling so a future regression in the worker's write path
is detected even without a live provider.

## Related

- Benchmark contract: `docs/benchmarks/README.md`.
- Schema note: ADR 0008 (three-slot adapter framework) and
  `docs/system-design.md §Storage primitives` (per-model vec tables).
