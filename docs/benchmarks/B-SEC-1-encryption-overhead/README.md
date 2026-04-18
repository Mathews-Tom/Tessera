# B-SEC-1 — Encryption-at-rest overhead baseline

**Goal:** quantify the sqlcipher tax against a plain SQLite baseline on the same schema so later retrieval-pipeline measurements have a reference point.

**Scope at P1 (this first baseline):**
- 1,000-facet synthetic vault (schema v1, no embeddings yet)
- Daemon-cold unlock p50 / p95 (sqlcipher `PRAGMA key` + first `SELECT`)
- Single-facet insert and lookup latency (500 trials each)

The full ratio-vs-B-RET-2 DoD claim is reserved for P12, when the retrieval pipeline is in place and the comparison has semantic weight. This harness establishes the measurement discipline and the first recorded number.

## Reproduce

From the repository root:

```bash
uv run python docs/benchmarks/B-SEC-1-encryption-overhead/run.py
```

Results land under `results/<utc-timestamp>.json`. The harness refuses to overwrite prior results, so every run produces a new file.

## Metric shape

Each result file records:

- `env` — OS, Python, sqlcipher/sqlite versions, git sha
- `inputs` — facet count, trial count, passphrase length
- `metrics.encrypted` — bootstrap ms, unlock p50/p95 ms, write/read p50/p95 ms
- `metrics.plain` — write/read p50/p95 ms against plain sqlite3
- `metrics.overhead` — encrypted/plain ratios for write and read at p50/p95

Write latencies include WAL commit; reads are indexed lookups by `external_id`.

## First baseline (2026-04-18, M-series macOS, sqlcipher 3.51.1)

| Metric | Encrypted | Plain | Overhead |
|---|---|---|---|
| Unlock p50 | 1.04 ms | — | well under the 500 ms DoD ceiling |
| Unlock p95 | 1.14 ms | — | — |
| Write p50  | 0.20 ms | 0.14 ms | 1.44x |
| Write p95  | 0.31 ms | 0.27 ms | 1.15x |
| Read p50   | 0.003 ms | 0.004 ms | 0.82x (sub-microsecond noise) |
| Read p95   | 0.003 ms | 0.004 ms | 0.86x |

Read overhead reports below 1.0 at this sample size because the per-row latencies are near the clock resolution ceiling; the retrieval-stage measurement in P12 will exercise real working-set sizes where the signal stabilizes.
