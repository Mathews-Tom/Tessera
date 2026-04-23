# B-RERANK-1 — Cross-encoder reranker latency baseline

**Goal:** quantify the per-query reranker latency for the reference
cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`) across candidate-set
sizes. The reranker runs once per `recall` call over the RRF-merged
top-50, so this number is a load-bearing component of the end-to-end
recall p95 target (`docs/release-spec.md §v0.1 DoD`).

**Scope at P2 (this first baseline):**
- Top-50 candidate set (the production candidate set).
- Top-10 candidate set (diagnostic — shows how latency scales with batch size).
- 100 trials per size, after a warm-up call that is discarded.
- CPU only. GPU determinism requires `CUBLAS_WORKSPACE_CONFIG` and is out of
  scope until a real user reports GPU need.

The full B-RERANK-1 cross-platform matrix (M1 / M2 / M3 Pro, Linux x86,
Windows) is finalised in P12 per the benchmark harness plan
(`.docs/development-plan.md §P12`). This baseline establishes the measurement
shape and records the contributor's reference machine so later platforms
have a comparison point.

## Reproduce

From the repository root:

```bash
uv run python docs/benchmarks/B-RERANK-1-reranker-latency/run.py
```

First run downloads the model weights (~90 MB) into the HuggingFace cache.
Subsequent runs hit the local cache and complete in under a minute.

Results land under `results/<utc-timestamp>.json`. The harness refuses to
overwrite prior results, so every run produces a new file.

## Metric shape

Each result file records:

- `env` — OS, arch, Python, torch version, device, git sha.
- `inputs` — reranker model, candidate-set sizes measured, trial count.
- `metrics.top_<n>` — p50, p95, p99, min, max, mean latency in milliseconds.

## First baseline (2026-04-18, macOS arm64, torch 2.11.0 CPU)

| Candidate set | p50 | p95 | p99 | mean |
|---|---|---|---|---|
| Top-10 | 22.0 ms | 23.7 ms | 24.2 ms | 22.1 ms |
| Top-50 | 80.1 ms | 89.1 ms | 92.1 ms | 81.3 ms |

The DoD target for reranker latency in the P2 exit gate is **p95 < 80 ms on
M-series** (`docs/release-spec.md §v0.1 DoD`). The top-50 p95 at 89 ms is
about 11% above the ceiling on the contributor's machine. The baseline is
recorded rather than tuned for three reasons:

1. The reference machine is a consumer laptop, not the M-series Pro hardware
   the DoD target assumes. The P12 cross-platform matrix will record M1 /
   M2 / M3 Pro numbers separately.
2. Cold-load is excluded from the samples, so the number above is per-call
   scoring latency on a warm model — the relevant figure for end-to-end
   recall latency.
3. Per `.docs/development-plan.md §P12`, B-RERANK-1 is documented in release
   notes rather than a shipping blocker. A regression against this number
   at P12 triggers a re-examination of the reranker path before ship.

First-call cold-load latency is excluded from the per-query sample — the
warm-up call is discarded.
