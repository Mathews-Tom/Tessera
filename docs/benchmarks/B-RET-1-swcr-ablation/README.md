# B-RET-1 — SWCR ablation

**Goal:** measure whether SWCR (Sequential Weighted Context Recall) clears the default-on acceptance thresholds in `docs/swcr-spec.md §Acceptance thresholds`. Outcome blocks the v0.1 shipping decision between default-on and opt-in.

**v0.1 outcome:** opt-in. See `docs/adr/0009-swcr-opt-in-pending-ablation.md` for the full decision trail.

## Arms

| Arm | Mode | Measures |
|---|---|---|
| A | `rrf_only` | Hybrid candidate generation + RRF fusion only. No rerank, no SWCR. |
| B | `rerank_only` | A + cross-encoder rerank. The P4 default. |
| C | `swcr` | B + SWCR coherence reweighting between rerank and MMR. |
| D | *(skipped)* | Cohere rerank v3 — requires licensed API key; out of scope here. |

## Dataset (S1, v0.1)

Seed-controlled synthetic vault. `dataset/generate.py` produces `s1.json` with N facets across 5 personas and 3 facet types (episodic, semantic, style). Each persona has a disjoint entity vocabulary; 30 % of facets also carry one of four ambient entities (`python`, `2026`, `slack`, `github`) that appear across personas as noise.

Default: 2 000 facets, 50 queries. The spec's target 10 000 / 50 rater-scored bundles is P12 territory; 2 000 is the session-scale first pass.

Reproduce:

```bash
uv run python docs/benchmarks/B-RET-1-swcr-ablation/dataset/generate.py \
    --n-facets 2000 --n-queries 50 --seed 0
```

## Metrics

| Metric | Definition |
|---|---|
| MRR@5 | Mean reciprocal rank of the first top-5 facet that belongs to the query's persona. |
| nDCG@5 | Normalised DCG with binary relevance (persona match). |
| persona-purity@5 | Fraction of the top-5 facets that belong to the target persona, averaged across queries. Named deliberately distinct from the spec's coherence-synthetic ("fraction of bundles where top-K facets all share at least one entity") because they compute different quantities. The strict coherence-synthetic metric lands alongside at v0.1.x. |
| latency p50 / p95 / p99 | End-to-end per-query pipeline latency. |

Human coherence (3 blind raters × 50 bundles × 5-point) is the spec's definitive gate. It is **deferred** here — it cannot be automated inside a single session and is the graduation gate for flipping the default.

## Reproduce

Fake adapters (deterministic, offline):

```bash
uv run python docs/benchmarks/B-RET-1-swcr-ablation/run.py
```

Real adapters (requires Ollama running locally with `nomic-embed-text` pulled, plus a populated HuggingFace cache for `cross-encoder/ms-marco-MiniLM-L-6-v2`):

```bash
uv run python docs/benchmarks/B-RET-1-swcr-ablation/run.py --adapters real
```

Results land under `results/<utc-timestamp>.json`. The harness refuses to overwrite.

## First results

### Fake adapters (hash embedder, keyword-overlap reranker)

| Arm | MRR@5 | nDCG@5 | persona-purity | p95 |
|---|---|---|---|---|
| A: RRF-only | 0.389 | 0.241 | 0.252 | 57.6 ms |
| B: RRF+rerank | 1.000 | 0.840 | 0.788 | 58.2 ms |
| C: RRF+rerank+SWCR | 0.980 | 0.858 | 0.816 | 59.2 ms |

- nDCG improvement C vs B: **+2.1 %** (fails `≥ +10 %`).
- MRR C vs B: **−2.0 %** (fails `no regression`).
- Latency: +1.75 % (passes).

### Real adapters (`nomic-embed-text` 768-dim, MiniLM cross-encoder)

| Arm | MRR@5 | nDCG@5 | persona-purity | p95 |
|---|---|---|---|---|
| A: RRF-only | 1.000 | 1.000 | 1.000 | 960 ms |
| B: RRF+rerank | 1.000 | 1.000 | 1.000 | 1121 ms |
| C: RRF+rerank+SWCR | 1.000 | 1.000 | 1.000 | 1093 ms |

- All three arms saturate at ceiling. The disjoint-persona entity vocabularies make the dataset too easy for 768-dim embeddings to get wrong.
- nDCG improvement = 0 % fails the `≥ +10 %` gate by ceiling effect, not SWCR weakness.
- MRR holds at 1.0 (no regression). Latency p95 is 28 ms **faster** with SWCR than rerank-only.

## What these results mean and do not mean

**What they mean.** The first-pass evidence does not support shipping SWCR default-on. The spec's own fallback applies (`§Acceptance thresholds` — "SWCR ships as opt-in if any threshold fails"). SWCR is available as `retrieval_mode: swcr` for opt-in use; the default is `rerank_only`.

**What they do not mean.** They do not show that SWCR is ineffective. The fake-adapter run is noise-dominated with the keyword-overlap reranker already at MRR=1.0. The real-adapter run has no headroom because the dataset is too separable for the embedder. Neither configuration probes the regime SWCR was designed for: cross-facet-type coherence under ambiguous cross-persona content.

**What a better ablation would change.**

- **Harder dataset.** Introduce cross-persona entity overlap (e.g. two personas both use `python` as a primary entity, not just ambient). Produce near-duplicate content across facet types within a persona so the rerank-alone pipeline is forced to pick one. Mix personas at query time (a query that belongs to two personas partially).
- **Human raters.** 3 blind raters × 50 `assume_identity` bundles × 5-point scale on "does this hang together as one agent's identity?" per the spec. This is the gate the automated proxies were designed to approximate; without it, the definitive evidence is absent.
- **Separate latency concern.** Real-adapter arm B at p95 = 1121 ms on 2K facets is already above the v0.1 DoD target of < 1 s at 10K. That is a scaling risk independent of SWCR — P12 benchmarks need to unpack whether the bottleneck is Ollama, the cross-encoder, or the pipeline orchestration.

## Revisit

Per ADR 0009, this benchmark runs again at v0.1.x with:

1. A harder S1′ dataset (cross-persona entity overlap, near-duplicate cross-type content).
2. Human-rater coherence scoring on 50 bundles × 3 raters.
3. Real adapters — the v0.1 DoD defaults.

If those results clear all four spec thresholds, the default flips to `retrieval_mode: swcr` in a v0.1.x release and ADR 0009 is superseded.
