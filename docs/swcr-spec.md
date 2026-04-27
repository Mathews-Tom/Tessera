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

- A set of user-context facets $F = \{f_1, \dots, f_n\}$ in a vault, each with a type $\tau(f)$ drawn from the stable facet vocabulary: $\{\text{identity}, \text{preference}, \text{workflow}, \text{project}, \text{style}\}$ in v0.1, extended with $\{\text{person}, \text{skill}\}$ at v0.3 and $\{\text{compiled\_notebook}\}$ at v0.5 (per ADR 0010).
- A query $q$ (free text) issued through the `recall` MCP tool, with an optional facet-type filter and a per-facet-type cap $k$. When the filter is omitted, `recall` operates **cross-facet by default** across every type the caller's token is scoped to read.
- A token budget $B$, distributed proportionally across the facet types in scope.

Return a ranked bundle $R \subseteq F$ such that:

1. **Relevance**: every $f \in R$ is topically aligned with $q$.
2. **Cross-facet coherence**: the facets in $R$ cross-reinforce across types. A style sample returned should match the register of the project facet also returned; a workflow returned should be the right shape for the project in scope; preferences returned should be applicable to the task implied by the query. This is the property classical retrieval does not model.
3. **Diversity per type**: within each facet type, $R$ covers the relevant space rather than returning near-duplicates.
4. **Budget**: serialized token count of $R$'s snippets is at most $B$, with each snippet ≤ 256 tokens.

Classical retrieval (BM25, dense nearest-neighbor, RRF fusion, cross-encoder rerank) optimizes (1) and partially (3). It does not model (2) — coherence across facet types — which is the load-bearing property for the T-shape cross-facet user-context bundle the product promises. SWCR is the stage that introduces (2) into an otherwise standard hybrid-retrieval pipeline.

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
- $\phi(f) \in \mathbb{R}^d$ — a "topology embedding" for $f$: in v0.1, the same dense embedding used for candidate generation. In v0.3+, augmented with an entity-presence vector sourced from the `person_mentions` table.
- $E(f) \subseteq \mathcal{E}$ — entities mentioned in $f$ (from metadata JSON in v0.1, from `person_mentions` in v0.3+).
- $\tau(f)$ — facet type, drawn from the ADR-0010 vocabulary.
- $w_\tau$ — per-type base weight. For v0.1 `recall`, all types in scope are weighted equally; the proportional per-facet-type token budget in `system-design.md §Retrieval pipeline` is the user-visible effect. v0.3+ may introduce per-query-shape weight hints (e.g., "draft" queries bias toward `style`), but v0.1 keeps weights uniform to avoid hand-tuned retrieval behavior before there is signal to tune against.

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

Intuition: a facet's SWCR score is its own relevance plus a boost proportional to how strongly it connects to other high-relevance candidates across the graph — especially across facet types. A style sample that aligns with the entities appearing in the project facets and with the workflow in scope is boosted; a style sample that is topically isolated from the rest of the candidate set is not.

### Per-facet-type budget enforcement (cross-facet `recall`)

`recall` is cross-facet by default. For a call with all five v0.1 facet types in scope and a total budget $B$, SWCR runs per-facet-type over its candidate subset, using the full top-$M$ cross-type candidate graph for the coherence term. The per-type envelope is set by the system-design spec — the token budget is distributed proportionally across the facet types the caller's token granted and the query engaged.

A representative v0.1 distribution with $B = 2000$ tokens and all five facets in scope:

```
per-facet envelope ≈ B / |types_in_scope|  ≈ 400 tokens per facet type
snippet cap                                    = 256 tokens
typical surfaced count                         = 1 snippet per facet type
```

A `recall` scoped to a single facet type gets the full $B = 2000$; SWCR still runs, but the coherence term on a single-type candidate set is a diversity bonus rather than a cross-type coherence bonus.

The prior `assume_identity` role-map (with hard per-role fractions for voice / recent-events / skills / relationships / goals) is retired with `assume_identity` itself in the April 2026 reframe. Coherence across facet types is delivered through the uniform cross-facet-default of `recall` plus the SWCR graph — not through a separate tool and separate code path.

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

All six are exposed via `config.yaml` under `retrieval.swcr`. A `retrieval_mode: rrf_only | rerank_only | swcr` switch controls the pipeline shape. **Default is `swcr`** (per ADR 0011). The non-default modes remain wired for ablation work and for users who want to reproduce pre-SWCR behavior without recompiling.

## Operational definition of coherence

Coherence in this specification means one testable property: **the cross-facet bonus term is the quantified expression of coherence.** There is no separate loss function, no constraint solver, no semantic validator. The claim is that facets that score well under $s_{\text{SWCR}}$ produce cross-facet bundles that human raters rate higher on the question "does this bundle hang together as one user's context across facet types?" than facets that score well under $s_r$ alone.

That claim is falsifiable. The primary evidence at v0.1 is the T-shape demo gate in `release-spec.md §v0.1 DoD`; the secondary quantitative evidence comes from the B-RET-1 ablation run on the harder v0.1.x dataset.

## Evidence gates and the B-RET-1 ablation

Per [ADR 0011](adr/0011-swcr-default-on-cross-facet-coherence.md), SWCR ships **default-on at v0.1**. Two distinct evidence artifacts track it:

### Primary gate — the T-shape demo (v0.1)

`release-spec.md §v0.1 DoD` names the T-shape cross-facet synthesis demo as the acceptance criterion for v0.1 ship:

> Fresh install → capture preference/workflow/project/style in one tool → open a different tool → `recall` returns a coherent cross-facet bundle → the second tool drafts in the user's voice using the right structure.

SWCR clears this gate when the cross-facet bundle is coherent enough that the downstream tool produces a draft the user recognizes as theirs across voice, structure, details, and rules of engagement. At least one real user (not Tom) must complete the demo unaided and the outcome must be recorded. This is a **qualitative capability gate**, not a statistical comparison — the ADR-0011 rationale explains why single-facet statistical comparisons on a single-persona dataset do not probe the cross-facet coherence regime SWCR was built for.

### Secondary gate — B-RET-1 as regression guard

The B-RET-1 ablation harness at `docs/benchmarks/B-RET-1-swcr-ablation/` retains its value as a **regression guard**:

- SWCR must not regress MRR@k vs. `rerank_only` at the 1% level on the S1 dataset. (The current B-RET-1 fake-adapter run at ‑2.0% MRR is attributed to saturation noise with the keyword-overlap reranker; a re-run on a harder dataset is expected to resolve this. The real-adapter run shows no MRR regression.)
- SWCR must not regress p95 latency vs. `rerank_only` beyond 15% (absolute 100 ms). Current B-RET-1 real-adapter measurements show SWCR is 28 ms faster at p95 — comfortably inside the bound.

### v0.1.x upgrade path — harder dataset, human raters

A v0.1.x ablation run introduces:

- **Harder S1′**: cross-persona entity overlap (two personas both use `python`, `github` as primary entities, not just ambient); near-duplicate content across facet types within a persona; queries that span two personas partially. This is the dataset that probes cross-facet coherence rather than disjoint-persona separability.
- **Human raters**: 3 blind raters × 50 `recall(facet_types=all)` bundles × 5-point scale on "does this hang together as one user's operating model across facet types?"
- **Target**: coherence-human mean ≥ 4.0 / 5 with ≥ +0.3 absolute improvement over `rerank_only` baseline.

If the v0.1.x run clears that target, the retrieval-depth moat claim can be added to `system-overview.md` as a separately-evidenced secondary differentiator. If it does not clear, SWCR remains default-on per ADR 0011 (the primary gate is the T-shape demo, not this statistical target), but the secondary moat claim does not get made.

### Failure modes the ablation must catch

- **Near-duplicate style samples in bundle** (diversity failure).
- **Style sample register mismatched to project facet register** (cross-facet coherence failure — this is the regime SWCR targets).
- **Workflow returned whose procedural shape does not match the project in scope** (workflow-project coherence failure).
- **Preference returned that contradicts the query's implied output type** (preference-query coherence failure).
- **Latency tail > 2 s** (budget enforcement failure).

## Non-goals of this specification

- SWCR is **not** a graph neural network. The reweighting is a closed-form matrix-vector operation, not a learned message-passing function.
- SWCR is **not** a replacement for rerank. It augments.
- SWCR does **not** attempt to be a federated-search algorithm across remote vaults. "Cross-facet" in Tessera means coherence across the five v0.1 facet types within a single user's single vault, not federation across multiple users or machines.

## Open questions

- **Per-facet-type rerankers.** A single cross-encoder tuned for QA may underperform on style. Evaluate at v0.3: separate reranker for style vs. other types.
- **Learned edge weights.** In v0.5+, fit (α, β, γ, λ) per user on implicit feedback (which bundles led to continued session vs. abandonment). Currently out of scope; risks privacy posture.
- **Temporal decay in the coherence graph.** Recent facets may deserve higher edge weights to other recent facets. Deferred to v0.5 (episodic temporal upgrades).
- **Cross-session coherence.** SWCR optimizes coherence within a single recall call (cross-facet, spatial). Coherence across sessions of the same project over weeks or months is a distinct property, currently delivered only through the typed-facet stability invariant. Deferred to v0.5 episodic temporal upgrades.

## References

- Cross-encoder reranking: Nogueira & Cho, "Passage Re-ranking with BERT" (2019).
- Reciprocal Rank Fusion: Cormack et al., "Reciprocal Rank Fusion outperforms Condorcet and individual rank learning methods" (SIGIR 2009).
- MMR diversification: Carbonell & Goldstein (1998).
- Dissertation chapter: _to be cited when public_.

## Revision history

| Version | Date    | Change                                                       |
| ------- | ------- | ------------------------------------------------------------ |
| Draft 1 | 2026-04 | Initial specification, parameters from prototype calibration |
