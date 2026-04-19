# ADR 0009 — SWCR ships opt-in at v0.1 pending ablation evidence

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews
**Supersedes:** none
**Revisit at:** v0.1.x graduation, after harder-dataset B-RET-1 run with human-rater scoring

## Context

`docs/swcr-spec.md §Acceptance thresholds for v0.1 default-on` names four gates SWCR must clear before shipping default-on:

1. Coherence-human mean ≥ 4.0 / 5 and ≥ 0.3 absolute improvement over the RRF+rerank baseline.
2. Bundle nDCG@k improvement ≥ 10 % over RRF+rerank on the S1 synthetic vault.
3. Latency p95 regression ≤ 15 % and ≤ 100 ms.
4. Zero regression on pure-relevance MRR@k.

If any gate fails, the spec mandates: "SWCR ships as opt-in with `retrieval_mode: rrf_rerank` as default, and the v0.1 moat claim is retracted pending further work."

P5 produced the first evidence from `docs/benchmarks/B-RET-1-swcr-ablation/`, run in two variants:

### Fake-adapter ablation (deterministic, offline)

16-dim hash embedder, keyword-overlap "reranker", 2K synthetic facets across 5 personas, 50 queries.

| Arm | MRR@5 | nDCG@5 | persona-purity@5 | p95 |
|---|---|---|---|---|
| A: RRF-only | 0.389 | 0.241 | 0.252 | 57.6 ms |
| B: RRF+rerank | 1.000 | 0.840 | 0.788 | 58.2 ms |
| C: RRF+rerank+SWCR | 0.980 | 0.858 | 0.816 | 59.2 ms |

- nDCG improvement C vs B: **+2.1 %** — fails `≥ +10 %` gate.
- MRR C vs B: **−2.0 %** — fails `no regression` gate.
- Latency p95 regression: **+1.75 %** — passes.
- Coherence-human: deferred (out of session scope).

### Real-adapter ablation (Ollama `nomic-embed-text` 768-dim + `cross-encoder/ms-marco-MiniLM-L-6-v2`)

Same dataset, same queries.

| Arm | MRR@5 | nDCG@5 | persona-purity@5 | p95 |
|---|---|---|---|---|
| A: RRF-only | 1.000 | 1.000 | 1.000 | 960 ms |
| B: RRF+rerank | 1.000 | 1.000 | 1.000 | 1121 ms |
| C: RRF+rerank+SWCR | 1.000 | 1.000 | 1.000 | 1093 ms |

- All three arms saturate at perfect recall. nDCG improvement = 0 % fails the `≥ +10 %` gate by ceiling effect, not by SWCR weakness.
- MRR holds at 1.0 with SWCR (no regression). Latency is **28 ms faster** with SWCR than rerank-only.

### Joint reading

- The S1 dataset with disjoint persona entity vocabularies is too easy for real 768-dim embeddings — arms cannot separate.
- The fake-adapter run has enough noise to separate arms but the weak keyword reranker already saturates MRR, leaving no headroom for SWCR to improve without floating-point noise showing as a small regression.
- Neither run provides evidence that SWCR clears the default-on thresholds. Neither run provides evidence that SWCR regresses meaningfully on real adapters. The algorithm is implemented, tested, and harmless; it is not yet proven superior.

## Decision

**SWCR ships as opt-in at v0.1.** The production default is `retrieval_mode: rerank_only`. The `swcr` mode remains fully wired and exercised by the test suite and benchmark harness; users who want it flip one config line.

Moat language in `docs/system-overview.md` and `docs/release-spec.md` is revised to match evidence: at v0.1 the moat is packaging (single-binary install) + ideology (all-local default, encryption-at-rest, no telemetry), not retrieval depth. SWCR graduates to default-on at v0.1.x when a harder B-RET-1 run clears the spec's thresholds, including the human-rater coherence gate.

## Rationale

1. **Shipping a default-on claim we cannot demonstrate is a credibility loss.** The first blog post that re-runs the ablation would surface the saturation / ceiling issue and the MRR regression in the fake-adapter run. Flipping default-on later with evidence in hand is a net gain; flipping from default-on to opt-in under external criticism is a net loss.
2. **The spec anticipates this path.** §Acceptance thresholds explicitly names opt-in shipping as the contingency; we are not improvising.
3. **The algorithm works.** It is implemented, tested, and latency-neutral-to-better under real adapters. Removing it would foreclose a v0.3 return when real user vaults + human raters finally give the coherence-human gate its actual signal.
4. **The right fix is dataset + raters, not algorithm tuning.** Iterating α/β/γ/λ on a saturating dataset until the numbers look right is p-hacking. The honest path is a harder S1 (cross-persona entity overlap, near-duplicate cross-type content) and 3 blind human raters on 50 `assume_identity` bundles. That work is post-P5.
5. **Cascading effect is small.** P6 (identity engine) and P8 (MCP surface) both accept `retrieval_mode` as config; they work unchanged with either default.

## Consequences

**Positive:**

- Release notes do not have to carry a claim that the ablation does not support.
- The opt-in mode lets ambitious users exercise SWCR without needing it blessed by the default-on thresholds first.
- P6 through P12 are unblocked; SWCR's default-on decision is decoupled from the v0.1 ship.

**Negative:**

- Loses the strongest narrative moat line ("topology-aware retrieval is genuinely deeper"). At v0.1 the case for the agent-identity category rests on framing + packaging, which is weaker. Funded competitors can reposition inside the narrative window.
- Opt-in users are a small fraction; the default-on claim is where the comparative evaluations happen. Until default-on flips, external reviewers treat Tessera as "clean packaging of standard retrieval."
- Opens a follow-up cost: the harder B-RET-1 dataset + human-rater scoring has to land by v0.1.x or the graduation stays blocked indefinitely.

## Alternatives considered

- **Ship SWCR default-on anyway.** The spec explicitly forbids this absent threshold clearance. Additionally, the real-adapter run showed no measurable SWCR improvement on the current dataset — a default-on claim with zero evidence is worse than no claim.
- **Delete SWCR from the codebase.** Rejected. The algorithm is correct, cheap, and latency-neutral. Deletion forecloses v0.3 reintroduction and costs more than keeping it behind a flag.
- **Iterate α/β/γ/λ until automated gates pass.** Rejected as p-hacking. The spec gates are external evidence, not knobs to tune against.
- **Rebuild S1 with cross-persona entity overlap this session.** Deferred. Dataset design work needs more care than a single session allows and belongs to the same follow-up as the human-rater gate.

## Revisit triggers

- Harder B-RET-1 dataset lands at v0.1.x with cross-persona entity overlap and near-duplicate cross-type content.
- 3 blind raters complete 50 `assume_identity` bundles × 5-point coherence scoring.
- If both of the above clear the spec thresholds (nDCG@k ≥ +10 %, MRR no regression, latency p95 regression ≤ 15 % / ≤ 100 ms, coherence-human ≥ 4.0 / 5 with ≥ +0.3 absolute): flip default to `retrieval_mode: swcr` in a v0.1.x release, restore moat language in `docs/system-overview.md`, and supersede this ADR with one recording the graduation evidence.
- If the harder ablation still does not clear, re-evaluate: either redesign SWCR parameter structure, scope out of v0.1.x, or retire.

## Related documents

- `docs/swcr-spec.md` — algorithm specification and ablation protocol.
- `docs/benchmarks/B-RET-1-swcr-ablation/` — harness, dataset, and two result files (fake and real adapters).
- `docs/system-overview.md §Moat` — revised moat order matching evidence.
- `docs/release-spec.md §Retrieval quality` — DoD items for opt-in vs graduation.
