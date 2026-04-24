# B-RET-1 — SWCR ablation (regression guard + cross-facet coherence probe)

**Goal under the April 2026 reframe.** B-RET-1 serves two distinct roles per [ADR 0011](../../adr/0011-swcr-default-on-cross-facet-coherence.md):

1. **v0.1 regression guard.** SWCR must not regress MRR@k vs. `rerank_only` at the 1% level, and must not regress p95 latency beyond 15% (absolute 100 ms). The current real-adapter run clears both bounds.
2. **v0.1.x cross-facet coherence probe.** A harder dataset (S1′, cross-persona entity overlap, near-duplicate cross-type content) + human raters produces the quantitative cross-facet coherence evidence that ADR 0011 deferred from v0.1.

**v0.1 outcome:** SWCR ships **default-on** per ADR 0011. The evidence gate is the T-shape demo in `release-spec.md §v0.1 DoD`, not the single-facet thresholds this benchmark was originally designed to test. The historical decision trail from the opt-in stance is preserved in [ADR 0009](../../adr/0009-swcr-opt-in-pending-ablation.md) (superseded).

## Arms

| Arm | Mode | Measures |
|---|---|---|
| A | `rrf_only` | Hybrid candidate generation + RRF fusion only. No rerank, no SWCR. |
| B | `rerank_only` | A + cross-encoder rerank. The P4 default. |
| C | `swcr` | B + SWCR coherence reweighting between rerank and MMR. |
| D | *(skipped)* | Cohere rerank v3 — requires licensed API key; out of scope here. |

The next serious run adds a `vector_only` arm. The current harness does not implement that mode.

## Dataset (S1, v0.1)

Seed-controlled synthetic vault. `dataset/generate.py` produces `s1.json` with N facets across 5 personas. The current generator produces three facet types; the generator will be updated to the v0.1 facet vocabulary (identity, preference, workflow, project, style) when the harness is re-aligned with the reframe. Each persona has a disjoint entity vocabulary; 30 % of facets also carry one of four ambient entities (`python`, `2026`, `slack`, `github`) that appear across personas as noise. The v0.1.x harder variant introduces cross-persona entity overlap to probe the regime SWCR targets.

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

## What these results mean and do not mean (pre- and post-reframe reading)

**Pre-reframe reading (superseded by ADR 0011).** Under the original framing, the ADR-0009 acceptance thresholds for single-facet MRR/nDCG improvement were not cleared, and SWCR was going to ship opt-in. That decision is superseded.

**Post-reframe reading (ADR 0011, the current position).** The S1 dataset in its current form is not the right instrument for measuring SWCR's cross-facet coherence contribution. The fake-adapter run is noise-dominated with the keyword-overlap reranker already at MRR=1.0. The real-adapter run has no headroom because the dataset with disjoint-persona entity vocabularies is too separable for 768-dim embeddings. Neither configuration probes the regime SWCR was designed for: cross-facet-type coherence under ambiguous cross-persona content. The v0.1 evidence gate moves to the T-shape demo (`release-spec.md §v0.1 DoD`); this benchmark survives as a regression guard (no MRR / latency regression vs. `rerank_only`) and becomes the primary quantitative instrument at v0.1.x when the harder dataset + human raters land.

**What a better ablation would change.**

- **Harder dataset.** Introduce cross-persona entity overlap (e.g. two personas both use `python` as a primary entity, not just ambient). Produce near-duplicate content across facet types within a persona so the rerank-alone pipeline is forced to pick one. Mix personas at query time (a query that belongs to two personas partially).
- **Cross-facet ambiguity.** Include queries where `style`, `project`, and `workflow` each point to plausible but different bundles, and score whether the final bundle coheres around the intended task rather than the nearest isolated fact.
- **Conflicting facts.** Include contradictory style and project facts with timestamps and provenance, then score whether retrieval prefers the newer or higher-scope fact without deleting evidence of the conflict.
- **Stale project context.** Include superseded project rows that remain semantically close to the query. The correct bundle should avoid stale project context when fresher context exists.
- **Multi-tool preference propagation.** Capture a preference from one tool (`claude-code`, `codex`, or `cursor`) and query from another. Score whether the preference survives the tool boundary and appears in the retrieved bundle when relevant.
- **Win-rate reporting.** Track pairwise bundle wins for SWCR against `vector_only`, `rrf_only`, and `rerank_only`, not only aggregate MRR/nDCG. SWCR's product claim is bundle coherence, so the eval needs a direct win-rate readout.
- **Human raters.** 3 blind raters × 50 `recall(facet_types=all)` bundles × 5-point scale on "does this hang together as one user's operating model across facet types?" per the reframed swcr-spec. This is the gate the automated proxies were designed to approximate; without it, the definitive evidence is absent.
- **Separate latency concern.** Real-adapter arm B at p95 = 1121 ms on 2K facets is already above the v0.1 DoD target of < 1 s at 10K. That is a scaling risk independent of SWCR — P12 benchmarks need to unpack whether the bottleneck is Ollama, the cross-encoder, or the pipeline orchestration.

## Revisit

Per [ADR 0011](../../adr/0011-swcr-default-on-cross-facet-coherence.md), this benchmark runs again at v0.1.x with:

1. A harder S1′ dataset (cross-persona entity overlap, near-duplicate cross-type content) across the full five-facet v0.1 vocabulary.
2. Human-rater coherence scoring on 50 `recall(facet_types=all)` bundles × 3 blind raters, 5-point "does this hang together as one user's operating model across facet types?" scale.
3. Real adapters (Ollama `nomic-embed-text` + sentence-transformers cross-encoder, per v0.1 DoD defaults).

If the harder run clears coherence-human ≥ 4.0 / 5 with ≥ +0.3 absolute improvement over `rerank_only`, the secondary retrieval-depth moat claim gets added back to `system-overview.md §Moat` as separately-evidenced. If it does not clear, SWCR remains default-on per ADR 0011 (the primary gate is the T-shape demo) but the secondary moat claim stays unmade.
