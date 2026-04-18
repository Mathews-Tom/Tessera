# ADR 0004 — Seven-facet identity model

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

Identity is not flat. A memory product can store facts as a single blob-of-notes; an identity product must distinguish between things-the-agent-remembers-happening, things-the-agent-knows, how-the-agent-sounds, what-the-agent-has-learned-to-do, who-the-agent-works-with, what-the-agent-is-trying-to-accomplish, and how-the-agent-weighs-tradeoffs.

Candidate models considered:

1. **Flat fact store** — one `facts` table, type inferred from content. Mem0's approach.
2. **Two-class split** — episodic vs. semantic only. Classical cognitive-science framing.
3. **Seven-facet decomposition** — episodic, semantic, style, skill, relationship, goal, judgment.
4. **Open-ended facet types** — user-definable, no schema enforcement.

## Decision

**Seven fixed facet types, stable across all versions**, introduced incrementally:

| Facet | Ships | Content | Retrieval lens |
|---|---|---|---|
| `episodic` | v0.1 | Events, decisions, conversations with timestamps | Time + semantic + entity |
| `semantic` | v0.1 | Facts the agent knows | Semantic + entity |
| `style` | v0.1 | Voice and writing samples | Semantic + diversity |
| `skill` | v0.3 | Learned procedures, markdown | Semantic + disk-syncable |
| `relationship` | v0.5 | Persistent model of who the agent works with | Entity-indexed |
| `goal` | v0.5 | Declared goals and values, time-bounded | Active-window |
| `judgment` | v1.0 | Trade-off patterns, weighted decisions | Context-matched |

The set is **closed**. Adding a new facet requires a public RFC and a major version bump.

## Rationale

1. **Identity is structurally non-flat.** Style is retrieved differently from episodic (diversity over relevance); skills are retrieved differently from semantic facts (procedure activation vs. fact lookup); judgments are retrieved differently from either (pattern matching over trade-off similarity). Flattening forces the retrieval pipeline to infer type from content, which is a harder problem than declaring type at capture time.
2. **`style` is the demo moment.** The most observable substrate-rupture symptom is the agent sounding different. Making style a first-class facet — not a retrieval lens over episodic — means `assume_identity` can guarantee voice samples are present in the bundle, regardless of what the semantic retrieval would have chosen. This is the v0.1 demo pivot.
3. **Closed set prevents vault fragmentation.** User-definable facet types sound flexible but in practice lead to inconsistent vaults that cannot be reasoned about by third-party tools, migrated cleanly, or exchanged between users. A stable vocabulary is a contract with the ecosystem.
4. **Incremental shipping reduces specification risk.** Ship 3, learn, then ship 4 more with real usage signal. Relationship, goal, and judgment designs benefit from actual user patterns observed at v0.3; shipping them in v0.1 would overconstrain the design.

## Orthogonality matrix

The seven facets are not perfectly orthogonal; they are as orthogonal as useful. The overlaps are known:

| Pair | Overlap | Resolution |
|---|---|---|
| episodic ↔ style | Every episodic event is also a writing sample | Capture as episodic; style is built from recent-N episodics' content AND explicit style captures |
| semantic ↔ skill | A fact about how to do something blurs into procedure | Semantic = static knowledge; skill = activatable procedure with pre/post conditions |
| relationship ↔ entity (v0.3) | Relationships are always about an entity | Relationship is the history + model; entity is the canonical reference |
| goal ↔ judgment | Judgments encode what was preferred; goals encode what is wanted | Goal is prospective, judgment is retrospective |

**`style` as first-class vs. retrieval lens.** The most-questioned decision. Style-as-lens (filter episodics for writing-sample-like content) is simpler but has three failure modes: (a) episodic content with timestamps and entity mentions is noisy as a voice sample; (b) retrieving style from episodic requires a second model call to score "is-this-a-voice-sample"; (c) `assume_identity` cannot guarantee voice presence in the bundle because style-as-lens produces zero-or-more results, not a dedicated slice. Style-as-facet gives the identity engine a guaranteed slot to fill.

## Consequences

**Positive:**
- `assume_identity` bundles are structured: voice + context + skills + relationships + goals are each a named slice with explicit token budget.
- Retrieval can route differently per facet type (e.g., MMR λ differs between style and semantic).
- Third-party tools can reason about vault contents without content classification.

**Negative:**
- CHECK constraint on `facet_type` is brittle across versions. Migrations extend the CHECK. Mitigated by documenting the full set at v0.1 schema time.
- Users capturing an ambiguous facet must choose a type. Documented in user-facing guide with examples.
- Adding an 8th facet is expensive (RFC + major bump). This is intentional.

## Alternatives considered

- **Flat fact store**: Wrong abstraction for identity. Rejected on architectural grounds.
- **Two-class (episodic + semantic)**: Insufficient to distinguish voice from content. Fails the demo.
- **Open-ended facet types**: Short-term flexibility, long-term fragmentation. Rejected.
- **Five-facet model (drop goal + judgment)**: Considered. Dropped because long-running autonomous agents without persisted goals behave drift-ward; judgment patterns are the differentiator for v1.0 multi-agent work.

## Revisit triggers

- Real users in v0.3+ consistently capture an eighth type that does not cleanly fit. File an RFC.
- The `style` vs. `episodic` overlap produces more confusion than utility. Consider merging.
- Judgment facet (v1.0) fails to improve agent continuity in blind evaluation. Consider removing from the contract.
