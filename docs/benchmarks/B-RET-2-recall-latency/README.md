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

The full DoD matrix — median `recall` < 500 ms at 10K facets, scaling
curves out to 100K — is finalised in P12 against the reference hardware
baseline defined in `docs/release-spec.md §v0.1 DoD` (MacBook Pro M1
Pro 10-core CPU / 16-core GPU, 16 GB RAM, macOS 15.x, daemon idle
except for the test query, no concurrent Ollama workload). This first
baseline proves the pipeline fits its target on small vaults and
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
