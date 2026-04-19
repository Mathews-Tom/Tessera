# B-WRITE-1 — Synchronous capture throughput baseline

**Goal:** quantify the capture-only write path against a fresh encrypted
vault. The P3 DoD requires capture latency p95 < 50 ms regardless of
embedder state; this harness records the capture path itself so later
numbers at 10K and 100K facets have a comparison point.

**Scope at P3 (this first baseline):**
- Fresh encrypted vault, schema v1, one agent.
- 500 sequential single-writer captures with distinct content.
- Measures wall-clock latency per capture call; embedding is deferred to
  the P3 embed worker and is NOT included in these samples.

The full P12 harness runs 10 concurrent MCP clients and records p99 <
200 ms at sustained ≥ 50 writes/sec. This single-writer baseline is
deliberately narrower and establishes the per-call latency shape.

## Reproduce

```bash
uv run python docs/benchmarks/B-WRITE-1-concurrent-capture/run.py
```

Results land under `results/<utc-timestamp>.json`. New runs produce new
files (the harness refuses to overwrite).

## Metric shape

- `env` — OS, arch, Python, git sha.
- `inputs` — trials, writer_count, facet_type.
- `metrics` — p50, p95, p99, min, max, mean latency in milliseconds, and
  derived mean writes/sec.
