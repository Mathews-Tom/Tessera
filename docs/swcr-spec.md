# SWCR — Sequential Weighted Context Recall

**Status:** Draft 1 (working specification — to be superseded when dissertation chapter lands)
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## Purpose of this document

SWCR is named throughout the Tessera documentation as the retrieval differentiator. Until now, the only public statements about SWCR have been narrative ("topology-aware," "multi-agent," "coherence across facet types"). That is insufficient. Retrieval code cannot be reviewed against narrative. This document specifies SWCR at a level that permits implementation, ablation, and rebuttal.

This is **working specification, not the final dissertation chapter.** The algorithm may change; parameters will be tuned against the benchmarks described in §Ablation protocol. This document is the source of truth for the Tessera codebase; the dissertation chapter, when it lands, supersedes.

## Problem statement

Given:

- A set of identity facets $F = \{f_1, \dots, f_n\}$ in a vault, each with a type $\tau(f) \in \{\text{episodic}, \text{semantic}, \text{style}, \text{skill}, \text{relationship}, \text{goal}, \text{judgment}\}$.
- A query $q$ (free text) or an identity-bundle request $Q$ (a structured request for an `assume_identity` bundle).
- A token budget $B$.

Return a ranked subset $R \subseteq F$ of size at most $k$ such that:

1. **Relevance**: every $f \in R$ is topically aligned with $q$ or with the role it plays in $Q$.
2. **Coherence**: the facets in $R$ cross-reinforce. Voice samples should match recent episodics; skills should match the entities and goals present; goals should match the trajectory implied by recent decisions.
3. **Diversity per role**: within each facet type, $R$ covers the space of relevant facets rather than returning near-duplicates.
4. **Budget**: serialized token count of $R$'s snippets is at most $B$.

Classical retrieval (BM25, dense nearest-neighbor, RRF fusion, cross-encoder rerank) optimizes (1) and partially (3). It does not model (2) — coherence across types.

## Algorithm

SWCR is a post-rerank reweighting stage that introduces a cross-facet coherence term into an otherwise standard hybrid-retrieval pipeline. It runs between cross-encoder reranking and MMR diversification.

### Pipeline placement

```text
query → candidate generation (BM25 + dense per type) → RRF fusion
      → cross-encoder rerank → [SWCR reweighting]
      → MMR diversification → token budget enforcement
```

SWCR is **not** a replacement for rerank; it is a cross-facet reweighting on reranked candidates. If rerank is disabled (degraded mode), SWCR operates on RRF-fused scores; the audit log records the degradation.

### Notation

- $s_r(f)$ — rerank score for facet $f$ (cross-encoder, already computed).
- $\phi(f) \in \mathbb{R}^d$ — a "topology embedding" for $f$: in v0.1, the same dense embedding used for candidate generation. In v0.3+, augmented with entity presence vector.
- $E(f) \subseteq \mathcal{E}$ — entities mentioned in $f$ (from metadata JSON in v0.1, from `entity_mentions` in v0.3+).
- $\tau(f)$ — facet type.
- $w_\tau$ — per-type base weight, set per tool (for `recall`, all types weighted equally by default; for `assume_identity`, roles are assigned per-type budgets).

### Coherence graph

Construct a weighted undirected graph $G = (F, W)$ over the top-$M$ reranked candidates ($M = 50$ default). Edge weight between $f_i$ and $f_j$:

$$
w(f_i, f_j) = \alpha \cdot \cos(\phi(f_i), \phi(f_j)) + \beta \cdot \frac{|E(f_i) \cap E(f_j)|}{|E(f_i) \cup E(f_j)| + \epsilon} + \gamma \cdot \mathbf{1}[\tau(f_i) \neq \tau(f_j)]
$$

where:

- $\alpha$ weights semantic similarity (default 0.5).
- $\beta$ weights entity overlap (default 0.3).
- $\gamma$ weights the cross-type bonus: edges between _different_ facet types contribute more, encoding that coherence is a cross-type property. Default 0.2.
- $\epsilon = 1$ to keep the Jaccard term finite when both entity sets are empty.

Self-loops are excluded. Edges with $w < 0.1$ are dropped (sparsification).

### SWCR score

For each candidate $f$, define:

$$
s_{\text{SWCR}}(f) = s_r(f) + \lambda \cdot \sum_{f' \in F \setminus \{f\}} w(f, f') \cdot s_r(f')
$$

where $\lambda$ is the coherence weight (default 0.25).

Intuition: a facet's SWCR score is its own relevance plus a boost proportional to how strongly it connects to other high-relevance candidates across the graph — especially across facet types. A voice sample that aligns with the entities appearing in recent episodics and with the active goal is boosted; a voice sample that is topically isolated from the rest of the candidate set is not.

### Per-type budget enforcement (assume_identity only)

For `assume_identity` with budget $B$, SWCR is invoked with a role map:

```
roles = {
  "voice":         {type: "style",        budget_fraction: 0.25, k_min: 3, k_max: 8},
  "recent_events": {type: "episodic",     budget_fraction: 0.30, k_min: 5, k_max: 15,
                    time_window_hours: configurable},
  "skills":        {type: "skill",        budget_fraction: 0.20, k_min: 2, k_max: 6},
  "relationships": {type: "relationship", budget_fraction: 0.15, k_min: 2, k_max: 5},
  "goals":         {type: "goal",         budget_fraction: 0.10, k_min: 1, k_max: 3},
}
```

For each role, SWCR is run over the candidate subset of matching type, using the full top-$M$ cross-type candidate graph for the coherence term. This ensures the voice samples returned are specifically the ones coherent with the recent-events and skills also being returned.

### Complexity

- Graph construction: $O(M^2 \cdot d)$ for the cosine terms ($M = 50$, $d \in \{384, 768, 1024\}$) = ~2.5M × $d$ FLOPs = negligible.
- Entity Jaccard: $O(M^2 \cdot |E_{\max}|)$ where $|E_{\max}|$ is the max entity-set size (bounded by metadata size cap).
- Reweighting: $O(M^2)$ lookups.

At $M = 50$: total overhead < 5 ms on M1 Pro. Not on the latency critical path.

## Parameters

| Parameter                     | Symbol | Default | Tunable range |
| ----------------------------- | ------ | ------- | ------------- |
| Semantic edge weight          | α      | 0.5     | [0.0, 1.0]    |
| Entity Jaccard weight         | β      | 0.3     | [0.0, 1.0]    |
| Cross-type bonus              | γ      | 0.2     | [0.0, 0.5]    |
| Coherence reweighting         | λ      | 0.25    | [0.0, 1.0]    |
| Edge sparsification threshold | τ_e    | 0.1     | [0.0, 0.3]    |
| Candidate set size            | M      | 50      | [20, 200]     |

All six are exposed via `config.yaml` under `retrieval.swcr`. A `retrieval_mode: rrf_only | rerank_only | swcr` switch disables SWCR entirely. Users can reproduce results from pre-SWCR baselines without recompiling.

## Operational definition of coherence

Coherence in this specification means one testable property: **the cross-facet bonus term is the quantified expression of coherence.** There is no separate loss function, no constraint solver, no semantic validator. The claim is that facets that score well under $s_{\text{SWCR}}$ produce bundles that human raters rate higher on the question "does this hang together as one agent's identity?" than facets that score well under $s_r$ alone.

That claim is falsifiable. The evidence lives in the ablation protocol.

## Ablation protocol

A SWCR claim without ablation is slideware. The following protocol is the minimum bar for shipping v0.1 with SWCR enabled by default.

### Dataset

- **Synthetic vault S1**: 10,000 facets, generated from 5 synthetic agent personas. Each persona has coherent voice, consistent entities, aligned goals. Content generated via a fixed pipeline (seed-controlled). Ground truth: for each `assume_identity` query, known-coherent facet subset.
- **Real vault R1**: Tom's own vault at time of shipping, anonymized. Realistic entity noise and type mix.

### Baselines

1. **RRF-only**: candidate generation + RRF fusion. No rerank, no SWCR.
2. **RRF + rerank**: add cross-encoder rerank. No SWCR.
3. **RRF + rerank + SWCR**: the proposed pipeline.
4. **RRF + rerank + Cohere rerank v3** (where permitted by licensing): strong off-the-shelf comparison point.

### Metrics

| Metric                  | Definition                                                                  |
| ----------------------- | --------------------------------------------------------------------------- |
| **Retrieval MRR@k**     | Mean reciprocal rank of known-coherent facets in S1 ground truth            |
| **Bundle nDCG@k**       | Normalized discounted cumulative gain on per-role bundles                   |
| **Coherence-human**     | 3 blind raters × 50 bundles × 5-point scale on "does this hang together?"   |
| **Coherence-synthetic** | Automated: fraction of bundles where top-K facets share at least one entity |
| **Latency p50 / p95**   | Per-query end-to-end on M1 Pro                                              |

### Acceptance thresholds for v0.1 default-on

SWCR ships enabled by default if and only if:

- Coherence-human mean ≥ 4.0 / 5 (and ≥ 0.3 absolute improvement over RRF + rerank baseline).
- Bundle nDCG@k improvement ≥ 10% vs. RRF + rerank on S1.
- Latency p95 regression vs. RRF + rerank ≤ 15% (absolute ≤ 100 ms).
- Zero regression on pure-relevance MRR@k (SWCR must not make individual-facet recall worse).

If any threshold fails, SWCR ships as opt-in with `retrieval_mode: rrf_rerank` as default, and the v0.1 moat claim is retracted pending further work.

### Failure modes the ablation must catch

- **Near-duplicate voice samples in bundle** (diversity failure).
- **Voice sample about entity X but recent episodics are entirely about entity Y** (cross-role coherence failure).
- **Skill whose preconditions are not present in the recent episodics** (activation failure).
- **Latency tail > 2 s** (budget enforcement failure).

## Non-goals of this specification

- SWCR is **not** a graph neural network. The reweighting is a closed-form matrix-vector operation, not a learned message-passing function.
- SWCR is **not** a replacement for rerank. It augments.
- SWCR does **not** attempt to be a multi-agent retrieval algorithm in the sense of federated search across remote vaults. Multi-agent in the Tessera sense means multiple facet types per agent, not multiple agents per query.

## Open questions

- **Per-facet-type rerankers.** A single cross-encoder tuned for QA may underperform on style. Evaluate at v0.3: separate reranker for style vs. other types.
- **Learned edge weights.** In v0.5+, fit (α, β, γ, λ) per user on implicit feedback (which bundles led to continued session vs. abandonment). Currently out of scope; risks privacy posture.
- **Temporal decay in the coherence graph.** Recent facets may deserve higher edge weights to other recent facets. Deferred to v0.5 (episodic temporal upgrades).

## References

- Cross-encoder reranking: Nogueira & Cho, "Passage Re-ranking with BERT" (2019).
- Reciprocal Rank Fusion: Cormack et al., "Reciprocal Rank Fusion outperforms Condorcet and individual rank learning methods" (SIGIR 2009).
- MMR diversification: Carbonell & Goldstein (1998).
- Dissertation chapter: _to be cited when public_.

## Revision history

| Version | Date    | Change                                                       |
| ------- | ------- | ------------------------------------------------------------ |
| Draft 1 | 2026-04 | Initial specification, parameters from prototype calibration |
