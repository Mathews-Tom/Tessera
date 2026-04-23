# ADR 0011 — SWCR default-on as cross-facet coherence primitive

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews
**Supersedes:** ADR 0009 (SWCR opt-in pending ablation)

## Context

ADR 0009 gated SWCR's default-on status behind four acceptance thresholds on the B-RET-1 ablation (MRR@k no regression, nDCG@k ≥ +10%, latency p95 regression ≤ 15%, coherence-human ≥ 4.0/5). The B-RET-1 results — both the fake-adapter and the real-adapter variants — did not clear those thresholds:

- Fake adapters: keyword-overlap reranker saturated MRR at 1.0, leaving no headroom.
- Real adapters: disjoint-persona entity vocabularies made the S1 dataset too separable for 768-dim embeddings; all three arms saturated at 1.0.
- Neither configuration probed the regime SWCR was designed for: cross-facet-type coherence under ambiguous cross-persona content.

ADR 0009 concluded "SWCR ships opt-in; graduate to default-on when a harder B-RET-1 run clears the thresholds."

The reframe (April 2026) changes the problem SWCR is evaluated against. Under the original framing, SWCR was a retrieval-depth differentiator competing against RRF+rerank on single-facet relevance metrics. Under the reframe, SWCR is the **cross-facet coherence primitive** that makes `recall` produce bundles aligned across facet types — style that matches project register, workflow relevant to the query, preferences applicable to the output, identity grounding the voice. That is not what the B-RET-1 ablation measures, and it is not what the ADR-0009 thresholds are calibrated against.

This means two things simultaneously:

1. The ADR-0009 evidence bar was calibrated for the wrong problem. Continuing to gate SWCR on single-persona MRR/nDCG improvements is category error after the reframe.
2. The reframe commits to SWCR as the load-bearing differentiator of the product (per `pitch.md`, `system-overview.md §Moat`, `system-design.md §Retrieval pipeline`, `release-spec.md §Retrieval pipeline`). Shipping it opt-in contradicts that positioning.

## Decision

**SWCR ships default-on at v0.1 as the cross-facet coherence weighting stage of the retrieval pipeline.** The production default is `retrieval_mode: swcr`. The `rerank_only` and `rrf_only` modes remain fully wired and exercised by the benchmark harness so ablations can still run; users who want to disable SWCR flip one config line.

The evidence gate moves from single-facet statistical thresholds to **the T-shape cross-facet demo** defined in `release-spec.md §v0.1 DoD`:

> Fresh install → capture preference/workflow/project/style in one tool → open a different tool → `recall` returns a coherent cross-facet bundle → the second tool drafts in the user's voice using the right structure.

SWCR clears this gate if and only if the cross-facet bundle produced is coherent enough that the second tool's draft feels like the user wrote it across voice, structure, details, and rules-of-engagement. This is a qualitative gate, verified by at least one real user (not Tom) completing the demo with no live help. It is not a statistical improvement over a baseline; it is a capability test of the whole pipeline.

The B-RET-1 ablation is retained but its role changes:

- **Regression guard, not acceptance gate.** B-RET-1 must show SWCR is not regressive vs. `rerank_only` on MRR@k and latency p95. It does not need to show improvement — the improvement SWCR delivers lives in cross-facet coherence, which B-RET-1 as currently designed does not measure.
- **Hardened variant pending.** A harder S1 dataset (cross-persona entity overlap, near-duplicate cross-type content) and human-rater coherence scoring remain v0.1.x work. When that run lands, the retrieval-depth claim can be added back as a separately-evidenced secondary moat. That is a v0.1.x enhancement, not a v0.1 ship blocker.

## Rationale

1. **The reframe changes the evaluation problem.** ADR 0009's thresholds were designed for a retrieval regime (single-facet relevance) that is no longer the product's load-bearing use case. Continuing to enforce them would gate a cross-facet-coherence product on single-facet-relevance evidence. That is evaluation mismatch, not engineering discipline.
2. **The demo is the product, and the demo is the gate.** If the T-shape demo works, SWCR is pulling its weight — the cross-facet coherence is what makes the demo land. If the demo does not work, no statistical improvement on B-RET-1 would save v0.1. The demo gate aligns evidence with what the product promises.
3. **Shipping opt-in contradicts the reframe's positioning.** The new `pitch.md`, `system-overview.md`, and `system-design.md` frame SWCR as the load-bearing differentiator that delivers cross-facet coherence. An opt-in flag behind that positioning is a credibility trap — users following the pitch would get flat retrieval by default.
4. **The algorithm is correct, cheap, and latency-neutral.** B-RET-1 real-adapter run shows SWCR at p95 is 28 ms *faster* than rerank-only (attributable to candidate-set effects within the reranker). Default-on has no latency cost and no demonstrable regression. The opposition to default-on was "no evidence it helps on our metric"; the response is "our metric was wrong for the reframed product."
5. **ADR 0009's revisit path was blocked indefinitely.** The graduation gate required a harder dataset + human raters that solo-dev pace could not deliver before v0.1 ship. The reframe provides a different evidence path (the T-shape demo) that is reachable in v0.1 scope.

## Consequences

**Positive:**
- Retrieval pipeline matches the pitch: cross-facet coherence is the default.
- Users and external reviewers get the product the docs describe, not a de-featured version.
- v0.1 ships the full retrieval stack; v0.1.x adds the hardened B-RET-1 evidence as a secondary moat, not as a precondition.

**Negative:**
- SWCR ships default-on without a statistical ablation clearing single-facet relevance thresholds. External reviewers who re-run B-RET-1 will find the same saturation/ceiling issues ADR 0009 described. The counter-narrative must be ready: the S1 dataset does not probe cross-facet coherence; the v0.1.x harder variant will.
- If the T-shape demo fails for reasons other than SWCR (embedder quality, reranker quality, schema problems), the fall-back position ("try SWCR opt-off, see if rerank-only does better") becomes the diagnostic, not the ship plan. Risk: if v0.1 demo failure routes to "SWCR is the problem," default-on becomes an embarrassment.
- v0.1 external claims must be carefully worded: "topology-aware cross-facet coherence weighting" is accurate; "measurably better than RRF+rerank on standard retrieval benchmarks" is not a claim v0.1 can defend until v0.1.x.

## Alternatives considered

- **Hold ADR 0009's position: SWCR ships opt-in.** Rejected. Contradicts the reframe's positioning and gates a cross-facet product on single-facet evidence.
- **Delete SWCR entirely and ship rerank_only as default.** Rejected. The reframe's load-bearing claim is cross-facet coherence; rerank_only does not deliver that. Removing SWCR would force a further reframe or require shipping a product that cannot deliver its central promise.
- **Re-run B-RET-1 with cross-facet coherence metrics before v0.1 ship.** Rejected at v0.1 scope. The harder S1 dataset and human-rater protocol cannot land in v0.1 timeline. Deferring v0.1 for that evidence is slower than shipping with the demo gate and backfilling the statistical evidence at v0.1.x.
- **Make SWCR opt-in but mark it as recommended in docs.** Rejected as the worst of both worlds: users follow defaults, so "recommended" opt-in means most users get flat retrieval while the pitch claims SWCR. Credibility trap.

## Revisit triggers

- v0.1 demo gate fails in internal or external testing and the root cause analysis points to SWCR producing incoherent bundles. Flip to `rerank_only` default, investigate SWCR parameters or dataset assumptions, open ADR 0012.
- Harder B-RET-1 at v0.1.x fails to clear cross-facet coherence thresholds. Re-examine SWCR parameter structure, consider dropping to opt-in until the algorithm is revised.
- An external reviewer's replication of B-RET-1 surfaces a regression on the existing single-facet metric that the default-adapter path of v0.1 inherits. Investigate whether the regression is real or saturation-dependent; if real, scope a fix.

## Related documents

- `docs/adr/0009-swcr-opt-in-pending-ablation.md` — superseded.
- `docs/swcr-spec.md` — algorithm specification; Problem statement reframed to match this ADR.
- `docs/benchmarks/B-RET-1-swcr-ablation/` — retained as regression guard; ADR 0009's acceptance-threshold interpretation superseded by this ADR.
- `docs/release-spec.md §v0.1 DoD` — the T-shape demo that gates SWCR's default-on ship.
- `docs/system-design.md §Retrieval pipeline` — describes SWCR as default coherence weighting stage.
