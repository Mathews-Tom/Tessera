# B-RET-3 — Cross-facet `recall` bundle assembly latency

> **Directory rename note.** This benchmark was originally named `B-RET-3-assume-identity` and measured the `assume_identity` tool. The April 2026 reframe (see [ADR 0010](../../adr/0010-five-facet-user-context-model.md)) retired `assume_identity`; the successor primitive is `recall(facet_types=all)`. The directory has been renamed; the benchmark ID `B-RET-3` is retained for continuity with prior references. The run harness (`run.py`) is scheduled for a code-level update to call `recall` instead of `assume_identity`; until that lands, results filed prior to 2026-04-23 measure the old path. Results filed after the harness update measure cross-facet `recall`.

**Goal:** quantify the `recall(facet_types=all)` bundle-assembly latency — the cross-facet T-shape retrieval that is the load-bearing primitive of Tessera's v0.1 product. The DoD target per `docs/release-spec.md §v0.1 DoD` is **median MCP `recall` latency < 500 ms with 10 000 facets in vault, all-local mode, on the reference hardware baseline** (MacBook Pro M1 Pro 10-core CPU / 16-core GPU, 16 GB RAM, macOS 15.x, daemon idle except for the test query, no concurrent Ollama workload).

**Scope at the first baseline:**

- 2 000 facets across the five v0.1 facet types (identity, preference, workflow, project, style). The full 10 000-facet scale with real Ollama + cross-encoder adapters is P12 territory.
- Deterministic fake hash embedder (8-dim) and length-based fake reranker. Isolates bundle-assembly cost from provider latency so the per-facet-type `asyncio.gather` shape is measurable in the noise floor.
- 100 trials after a warm-up call.

## Reproduce

```bash
uv run python docs/benchmarks/B-RET-3-cross-facet-coherence/run.py
```

## Metric shape

- `env` — OS, arch, Python, git sha.
- `inputs` — facet counts per type, dim, trial count, adapter identifiers, token budget, facet-type scope.
- `metrics` — p50 / p95 / p99 / min / max / mean latency in milliseconds for end-to-end cross-facet bundle assembly.

## Revisit

P12 runs this at 10 000 facets with Ollama (`nomic-embed-text`) plus the `cross-encoder/ms-marco-MiniLM-L-6-v2` reranker to validate the DoD target against production adapters on the reference hardware baseline. The harness must be updated to call `recall(facet_types=all)` before the P12 run is meaningful.

## Related

- [ADR 0010 — Five-facet user-context model](../../adr/0010-five-facet-user-context-model.md)
- [ADR 0011 — SWCR default-on as cross-facet coherence primitive](../../adr/0011-swcr-default-on-cross-facet-coherence.md)
- `docs/release-spec.md §v0.1 DoD` — the cross-facet coherence demo that is the primary v0.1 evidence gate.
- `docs/system-design.md §Retrieval pipeline` — the pipeline stages this benchmark measures end-to-end.
