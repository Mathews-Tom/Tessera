# B-RET-2 — Retrieval latency baseline at 1K facets

**Goal:** record end-to-end pipeline latency (BM25 + dense + RRF + rerank
+ MMR + budget) against a 1K-facet synthetic vault with deterministic
fake adapters. The measurement isolates the pipeline cost from provider-
side embedding latency so later numbers against live Ollama at 10K and
100K facets attribute the delta to the provider, not to the storage or
the pipeline itself.

**Scope at P4 (this first baseline):**
- 1K episodic facets, 8-dim hash-based fake embeddings.
- 100 trials after a warm-up call that is discarded.
- Single-writer, single-reader; sequential per-query path.

The full DoD matrix — p50 < 500 ms / p95 < 1 s at 10K, p50 < 1 s / p95
< 2.5 s at 100K on M1 Pro with real providers — is finalised in P12.
This baseline proves the pipeline fits its target on small vaults and
records the shape for later comparison.

## Reproduce

```bash
uv run python docs/benchmarks/B-RET-2-recall-latency/run.py
```

Results land under `results/<utc-timestamp>.json`. New runs produce new
files.

## Metric shape

- `env` — OS, arch, Python, git sha.
- `inputs` — facet count, dim, trial count, embedder / reranker
  identifiers, retrieval mode.
- `metrics` — p50, p95, p99, min, max, mean latency in milliseconds.
