# ADR 0010 — Five-facet user-context model

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews
**Supersedes:** ADR 0004 (seven-facet identity model)

## Context

Tessera was originally framed as an agent-identity layer: persistent state that lets an autonomous agent survive substrate swaps (model changes, provider changes). ADR 0004 decomposed that identity into seven facets — `episodic`, `semantic`, `style`, `skill`, `relationship`, `goal`, `judgment` — justified by the distinct retrieval semantics each needs when assembling an identity bundle for an agent that has just been re-instantiated on a new substrate.

The reframe (April 2026, captured in the post-reframe versions of `pitch.md`, `system-overview.md`, `system-design.md`, `release-spec.md`) shifts the unit of analysis from **agent identity** to **user context**. The user is the lead actor. Tessera is the portable context layer that travels with the user across every MCP-speaking AI tool they use. Tools come and go; context persists.

This shift invalidates ADR 0004's decomposition for three reasons:

1. **The user is T-shaped, not autonomous.** The archetypal user has deep vertical expertise in one or two domains and active horizontal engagement across many others through AI tools. Their context is not "what has this agent experienced" — it is "who I am, how I prefer to work, what I'm working on, how I sound." The taxonomy must serve the T-shape, not the substrate-swap story.
2. **The load-bearing call changed.** ADR 0004's central use case was `assume_identity` — produce a bundle that re-hydrates a new substrate into the prior agent. The reframe retires that tool. The load-bearing call is `recall`, which assembles a cross-facet bundle so any tool can draft in the user's voice with the user's workflows on the user's active projects. The facet taxonomy must serve `recall`, not `assume_identity`.
3. **`episodic` and `judgment` are premature abstractions.** Both were included in the seven-facet model because long-running agents need them. For a user asking ChatGPT to draft a LinkedIn post, neither contributes. Shipping facet types nobody writes to is architectural dead weight.

## Decision

**Five facet types in v0.1, expanded to seven in v0.3, with one additional type in v0.5:**

| Facet | Ships | Content | Retrieval lens |
|---|---|---|---|
| `identity` | v0.1 | Stable-for-years facts about the user (role, domains, locale) | Semantic + stability weight |
| `preference` | v0.1 | Stable-for-months behavioral rules (`uv` over `pip`, no emojis) | Semantic + entity |
| `workflow` | v0.1 | Procedural patterns (LinkedIn 5-act structure) | Semantic + procedure-shape |
| `project` | v0.1 | Active work context (what the user is currently building) | Semantic + recency |
| `style` | v0.1 | Writing voice samples (LinkedIn posts, Reddit comments) | Semantic + diversity |
| `person` | v0.3 | Persistent model of individuals the user works with | Entity-indexed |
| `skill` | v0.3 | Learned procedures stored as `.md` content | Semantic + disk-syncable |
| `compiled_notebook` | v0.5 | Write-time synthesized artifacts for vertical-depth topics | Freshness-gated |

The v0.1 set is closed. Adding a v0.3 or v0.5 facet type requires a bump in the `facet_type` CHECK constraint and a documented migration. The v0.3 and v0.5 types are **reserved in the v0.1 schema** (CHECK constraint lists them) so introducing them is additive, not a schema rewrite.

**Write-time policy.** `compiled_notebook` is the only write-time facet type. Its `mode` column value is `write_time`; the compilation agent is the only writer. The five v0.1 facets and the v0.3 `person`/`skill` types are `query_time` exclusively. There is no per-facet user-facing mode toggle on existing facet types — the `mode` column records the row's production method, not a user choice. If real-user signal after v0.5 calls for a per-facet mode toggle on existing types, it becomes a later decision.

## Rationale

1. **Five facets cover the T-shape cleanly.** Every facet maps to a category of real-world ask: "who I am" (identity), "how I want you to behave" (preference), "what procedure to follow" (workflow), "what I'm working on" (project), "how I sound" (style). Every category is needed for the v0.1 demo; none can be dropped without breaking the cross-facet synthesis story.
2. **Seven facets at v0.3 are user-signal-driven, not pre-planned.** `person` and `skill` ship in v0.3 if v0.1 usage justifies them. The ADR-0004 model pre-committed to seven facets at design time; the reframe commits to five at design time and seven when real users write to them.
3. **`compiled_notebook` replaces per-facet Framing-Y.** The alternative design — a per-facet user-facing toggle between `query_time` and `write_time` on all existing facets — is rejected because for `preference`, `workflow`, `identity`, and `style` the toggle would have no user-visible benefit. A preference compiled at write-time is still a preference; the user wants it applied, not synthesized. Only the vertical-depth case (long-running research, evolving deep domain thinking) has genuine write-time value, and that case is served cleanly by a dedicated facet type.
4. **Closed-at-v0.1 set prevents vault fragmentation.** User-definable facet types are tempting but produce vaults that third-party tools cannot reason about. A stable vocabulary is a contract with the ecosystem.
5. **Dropping `episodic`, `judgment`, and `goal` is honest scope discipline.** The ADR-0004 model included them because autonomous agents need them; v0.1 users are humans driving AI tools, not autonomous agents. If a v1.0 multi-agent future calls for them, the schema extends. Shipping them in v0.1 is over-commitment to a use case the reframe no longer serves.

## Mapping to ADR 0004

| ADR 0004 facet | ADR 0010 disposition |
|---|---|
| `episodic` | Dropped. Conversation history is imported into the five facets via v0.3 importers; it is not a first-class facet. Temporal queries are a v0.5 stretch if user signal warrants. |
| `semantic` | Split into `identity` (stable-for-years), `preference` (stable-for-months), and `project` (active-state). The undifferentiated "semantic facts the agent knows" category is replaced by three time-scale-specific categories. |
| `style` | Unchanged. Retained verbatim. |
| `skill` | Unchanged in intent, deferred to v0.3 (same target as ADR 0004). |
| `relationship` | Replaced by `person` at v0.3. The new name is clearer (`person` is a noun users can write into; `relationship` is a property). |
| `goal` | Dropped. Active goals live in `project` facets; declared long-term aspirations are a v1.0 question if they arise. |
| `judgment` | Dropped. Trade-off patterns are an autonomous-agent concept; v0.1–v1.0 do not commit to them. |

## Consequences

**Positive:**
- v0.1 ships a cleaner, demo-coherent taxonomy that directly matches the T-shape narrative.
- `recall` cross-facet synthesis has a concrete semantic: pull the relevant slice from each of the five categories a real query crosses.
- Schema `mode` column and `compiled_notebook` reservation make v0.5 additive.

**Negative:**
- Any pre-reframe vaults written under the seven-facet schema would need migration. In practice the reframe predates shipped code under the seven-facet schema, so the migration is theoretical. If a pre-reframe vault exists: `episodic` rows import as dated `project` facets; `semantic` splits by content cue; `relationship`/`goal`/`judgment` rows export to `.md` and drop.
- The ADR-0004 orthogonality matrix is discarded. The new model's orthogonality argument is simpler: each of the five facets is a different time-scale and lifecycle (years / months / procedural / active / voice).

## Alternatives considered

- **Keep the seven-facet model.** Rejected because it was built for the agent-identity framing the reframe retires. Shipping `goal` and `judgment` for a v0.1 user base that doesn't write to them is architectural dead weight.
- **Ship three facets only (identity, preference, style) and derive the rest.** Rejected because `workflow` and `project` are load-bearing for the demo. Cross-facet synthesis requires all five to produce the "drafts feel like me" moment.
- **Open-ended user-defined facet types.** Rejected. Short-term flexibility, long-term fragmentation.
- **Six facets (add `compiled_notebook` at v0.1).** Rejected. Write-time compilation needs real users to shape the compiler; shipping an empty facet type in v0.1 is premature.

## Revisit triggers

- Real users in v0.3+ consistently capture an eighth category that does not cleanly fit one of the eight planned types. File an RFC.
- `person` or `skill` at v0.3 produces retrieval quality worse than expected. Consider merging `person` back into `project` metadata, or moving `skill` out of the vault into disk-only storage.
- Write-time compilation at v0.5 produces artifacts users never query. Consider retiring `compiled_notebook` and reclaiming the `mode` column.

## Related documents

- `docs/adr/0004-seven-facet-identity-model.md` — superseded.
- `docs/adr/0011-swcr-default-on-cross-facet-coherence.md` — SWCR's role under the new model.
- `docs/system-design.md §The five-facet context model` — prose framing.
- `docs/release-spec.md §v0.1` — ship list matching this ADR.
- `docs/swcr-spec.md` — retrieval algorithm consuming these facet types.
