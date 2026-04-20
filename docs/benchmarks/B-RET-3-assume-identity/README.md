# B-RET-3 — assume_identity latency

**Goal:** quantify the `assume_identity` bundle-assembly latency against a
synthetic two-facet-type vault. The DoD target per `docs/release-spec.md
§Performance` is p50 < 1.5 s, p95 < 3 s at 10K facets on M1 Pro
returning a 6K-token bundle.

**Scope at P6 (this first baseline):**
- 2K facets total (500 style + 1500 episodic). The full 10K scale is
  P12 territory alongside real-adapter runs.
- Deterministic fake hash embedder (8-dim) and length-based fake
  reranker. Isolates bundle-assembly cost from provider latency so the
  per-role asyncio.gather shape is measurable in the noise floor.
- 100 trials after a warm-up call.

## Reproduce

```bash
uv run python docs/benchmarks/B-RET-3-assume-identity/run.py
```

## Metric shape

- `env` — OS, arch, Python, git sha.
- `inputs` — facet counts per type, dim, trial count, adapter
  identifiers, tool budget, recent window hours.
- `metrics` — p50 / p95 / p99 / min / max / mean latency in ms.

## Revisit

P12 runs this at 10K facets with Ollama (`nomic-embed-text`) plus the
`cross-encoder/ms-marco-MiniLM-L-6-v2` reranker to validate the DoD
target against production adapters.
