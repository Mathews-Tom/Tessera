# B-EMB-1 — Async embed throughput baseline

**Goal:** establish the worker-path cost independent of provider
latency. With a zero-latency fake embedder, the recorded throughput is
dominated by vec-table writes, transaction commits, and the worker
state-machine updates — the pieces that will still cost time when the
real provider is fast.

**Scope at P3 (this first baseline):**
- 500 captured facets, 8-dim vectors, one registered embedding model.
- Zero-latency in-process fake embedder.
- Single-pass drain via repeated `embed_worker.run_pass` calls.

The operational DoD — "no facet lingers pending > 10 min under nominal
load; Ollama restart recovery within 60 s" — is measured against a
live Ollama restart sequence in P12.

## Reproduce

```bash
uv run python docs/benchmarks/B-EMB-1-async-embed/run.py
```

Results land under `results/<utc-timestamp>.json`. New runs produce new
files.

## Metric shape

- `env` — OS, arch, Python, git sha.
- `inputs` — facets, dim, embedder identifier.
- `metrics` — capture elapsed + throughput, embed elapsed + throughput,
  total embedded count.
