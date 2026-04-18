# Tessera — Benchmarks

**Status:** Reserved — to be populated during v0.1 development
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## Purpose

Tessera makes performance and correctness claims that ship in the definition of done (see `release-spec.md`). Those claims are testable only if the benchmarks are reproducible. This directory is the home for:

1. Benchmark scripts and datasets.
2. Recorded results (as JSON, reviewable in PRs).
3. Write-ups that reference the recorded results.

No claim in the Tessera docs stands without a corresponding benchmark here.

## Required benchmarks for v0.1

| ID          | Benchmark                                                                             | Why it matters                                   | Target metric                                                                                 | Shipping requirement                                                        |
| ----------- | ------------------------------------------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| B-RET-1     | SWCR vs. RRF-only vs. RRF+rerank vs. RRF+rerank+SWCR                                  | Ablation for the central retrieval claim         | Coherence-human ≥ 4.0/5, nDCG@k ≥ +10%                                                        | Must pass before SWCR is default-on (see `swcr-spec.md §Ablation protocol`) |
| B-RET-2     | Retrieval latency at 1K / 10K / 100K facets                                           | DoD claim: <500 ms recall at 10K                 | p50 < 500 ms, p95 < 1 s at 10K                                                                | DoD gate for v0.1                                                           |
| B-RET-3     | `assume_identity` latency + bundle coherence at 10K facets                            | DoD claim: <1.5 s at 10K                         | p50 < 1.5 s, p95 < 3 s; coherence-human ≥ 4.0                                                 | DoD gate for v0.1                                                           |
| B-WRITE-1   | SQLite concurrent-capture throughput                                                  | Multi-agent writes, WAL checkpoint contention    | Sustained ≥ 50 writes/sec at p99 < 200 ms, 10 concurrent writers                              | DoD gate for v0.1                                                           |
| B-EMB-1     | Async embed throughput; failure and recovery                                          | Capture returns immediately; embeds happen async | No facet lingers unembedded > 10 min under nominal load; recovery after Ollama restart < 60 s | DoD gate for v0.1                                                           |
| B-REEMBED-1 | End-to-end re-embed wall time for 10K / 100K facets; recall quality during transition | Embedder-swap story is load-bearing              | 10K in < 10 min on M1 Pro with Ollama + nomic-embed-text; shadow-query degrades gracefully    | DoD gate for v0.1                                                           |
| B-RERANK-1  | Cross-encoder reranker latency per platform                                           | Degraded-mode fallback condition                 | p95 < 80 ms on M1/M2/M3 Pro; documented on Linux x86, Windows                                 | Documented in release notes, not a blocker                                  |
| B-SEC-1     | Encryption-at-rest overhead                                                           | sqlcipher tax on read/write paths                | ≤ 15% overhead on B-RET-2; decrypt-on-open < 500 ms                                           | DoD gate for v0.1                                                           |
| B-SEC-2     | Outbound-network block test                                                           | No hidden outbound calls                         | Full test suite passes with all outbound blocked except configured adapters                   | DoD gate for v0.1                                                           |

## Method — for every benchmark

Each benchmark script must:

1. Declare its environment in a `env.json`: OS, kernel, CPU, RAM, Ollama version, embedder model + revision, reranker model + revision, Python version, Tessera git sha.
2. Be reproducible from a single shell command (`make bench-<ID>`).
3. Write results as a JSON document with: `benchmark_id`, `timestamp`, `env`, `inputs`, `metrics`, `samples` (raw measurements or path to them).
4. Include a minimum sample size appropriate to the metric (latency: ≥ 500 trials; coherence-human: ≥ 50 bundles × ≥ 3 raters).
5. Refuse to overwrite prior results; new runs produce new files under `results/<benchmark_id>/<timestamp>/`.

## Review policy

A documentation change that claims a performance number must link to a benchmark result file in the same PR. Claims in `system-design.md`, `release-spec.md`, and `pitch.md` are subject to this rule. Claims in `system-overview.md` may cite qualitative observations only when the quantitative counterpart is not yet measured, and must mark them as such.

## Directory layout (to be created during v0.1 dev)

```text
docs/benchmarks/
├── README.md                       (this file)
├── B-RET-1-swcr-ablation/
│   ├── run.py
│   ├── dataset/
│   └── results/<timestamp>/result.json
├── B-RET-2-recall-latency/
├── B-RET-3-assume-identity/
├── B-WRITE-1-concurrent-capture/
├── B-EMB-1-async-embed/
├── B-REEMBED-1-embedder-swap/
├── B-RERANK-1-reranker-latency/
├── B-SEC-1-encryption-overhead/
└── B-SEC-2-outbound-block/
```

## What NOT to benchmark

- **Model quality of embedders or rerankers in isolation.** Tessera takes them as-is from their providers. Benchmark the pipeline, not the model.
- **Ollama throughput as a library.** Out of scope; benchmark Tessera's call pattern.
- **Microbenchmarks of SQLite internals.** Benchmark Tessera's queries, not SQLite.
