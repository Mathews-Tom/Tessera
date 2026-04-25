# ADR 0012 — v0.3 People + Skills design

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews
**Implements:** `release-spec.md §v0.3` (post-v0.1.x graduation surface)

## Context

The v0.3 surface adds two new first-class concepts to Tessera's vault and MCP boundary: **people** (the colleagues, peers, and public figures the user works with regularly) and **skills** (named procedures the user has authored, syncable to disk as Markdown files). Schema v3 reserves the columns and tables both need; the question this ADR records is *how* the two surfaces are shaped at the application boundary — what is a row, what is a facet, what is auto-derived, and what stays user-driven.

Three design tensions surfaced during implementation:

1. **People as facets vs. people as rows.** ADR 0010 admits `person` as a facet type in the schema CHECK, which suggests storing people as facets in the `facets` table. But people accumulate aliases, get merged when the user disambiguates "Sarah" and "Sarah Johnson", and need a relationship-graph link to facets that mention them. A facet-typed person row would carry that state through metadata and content_hash, blurring the line between identity facts and the relationship graph.
2. **Skill granularity.** A skill could be one procedure per facet (the simple model) or a structured object with `name`, `description`, `procedure_md`, `active`, and an optional `disk_path`. The second is richer but pushes skill-shaped fields into a generic `metadata` JSON blob.
3. **Resolution policy for ambiguous mentions.** When the user writes "Sarah said the API was flaky", the system needs to map "Sarah" to a person row. With multiple Sarahs, the choice is: auto-pick (most-mentioned, most-recent), surface candidates and let the agent disambiguate with the user, or refuse to resolve.

## Decision

**People are stored as separate rows in a `people` table, not as facets.** The schema `facet_type='person'` value remains in the CHECK only for legacy migration paths (ADR 0010 referenced it before v0.3 design) and is not produced by any v0.3 write surface. The `people` table carries `canonical_name`, a JSON `aliases` array, and `metadata`; the relationship graph lives in `person_mentions(facet_id, person_id, confidence)` with `ON DELETE CASCADE` on both foreign keys so erasing a facet or merging a person automatically tidies the link table.

**Skills are facets with `facet_type='skill'`** plus a structured metadata schema (`{"name": "git-rebase", "description": "...", "active": true}`) and an optional `disk_path` column. The `content` field carries the procedure markdown verbatim. The schema-level partial unique index `(agent_id, disk_path) WHERE disk_path IS NOT NULL AND is_deleted = 0` keeps each disk file mapped to at most one live skill row.

**Resolution is conservative.** `vault.people.resolve(mention)` returns a `ResolveResult(matches, is_exact)` shape: a single canonical-name or alias match flips `is_exact=True`; multi-match or substring/prefix hits return every candidate with `is_exact=False`. The MCP `resolve_person` tool surfaces the candidate list directly to the agent, which is then responsible for asking the user when ambiguous. Auto-pick policies (most-mentioned, most-recent) are not wired in v0.3 — they push disambiguation policy into the protocol when it belongs in the agent's UX layer.

**Importers backfill v0.1 facet types only.** The ChatGPT and Claude conversation-history importers write `project` (the default) or any other v0.1 type the caller chooses — never `skill`, `person`, or `compiled_notebook`. Skills are user-authored through `learn_skill`; people surface through `resolve_person`. Person-mention auto-extraction during import is documented as future work but not shipped at v0.3 — heuristic NER without ground-truth data over-engineers a problem real-user mistakes haven't yet justified.

**Recall fans out over `skill` by default; `person` is excluded.** The `_DEFAULT_RECALL_TYPES` set adds `skill` (a facet type with rows in `facets`) but not `person` (people live in their own table; `recall` operates on `facets`). The v0.3 spec line "recall includes top-K people and skills" maps to "skills appear in cross-facet bundles by default; people surface via the dedicated `resolve_person` tool".

## Rationale

1. **Separating people from facets respects the data shape.** Facets are content-hash-deduplicated immutable rows; people accumulate aliases and merge over time. Forcing people into the facets table would either fight content-hash dedup (every alias change rewrites the row) or store the alias graph in `metadata` (where SQL queries against it become awkward `json_extract` joins). A separate table is the simpler shape.
2. **Skills as structured facets capture the right invariant.** The `name` is a user-facing identifier and must be unique per agent; the `procedure_md` is content and dedups via `UNIQUE(agent_id, content_hash)`. Storing `name` in metadata rather than a dedicated column trades one SQL join (`json_extract(metadata, '$.name')`) for keeping the facets table schema-stable across v0.3 / v0.5 / v1.0 — adding a `skill_name` column to `facets` would force v0.5 / v1.0 facet types to either populate or NULL it.
3. **Conservative resolution is the right v0.3 default because we have no calibration data.** Auto-pick heuristics (most-mentioned, most-recent, alias-confidence-weighted) all assume a steady-state corpus where one Sarah Johnson is dominant. At v0.3, the corpus is empty or near-empty — the user's first ten captures don't tell us which Sarah is the "real" one. Surfacing candidates and letting the agent ask the user is the only policy that can't be wrong; auto-pick can.
4. **Importers writing v0.1-only types preserves user authorship.** If the ChatGPT importer auto-derived skills from past conversations ("you asked about git rebase 12 times, here's a skill"), the user's skill list would be cluttered with system-inferred entries the user never authored. The spec is explicit that skills are user-authored; importers respect that line.
5. **Person rows are not facets at the recall layer because they are not embedded.** The retrieval pipeline runs on facet vectors stored in `vec_<id>` tables. People rows have no embeddings. Including `person` in the default recall fan-out would query a facet_type with zero rows under the embedding index. The `resolve_person` tool serves the lookup-by-name use case directly without faking it through the recall pipeline.

## Consequences

**Positive:**

- The schema's facet-type CHECK admits `person`, `skill`, and `compiled_notebook` per ADR 0010, but the v0.3 application layer only writes `skill`. Forward-compatibility is preserved without forcing an unused write path through `vault.facets.insert`.
- Skill round-trip to disk is a clean Markdown sync: the `content` field is the file body, `disk_path` links them, the partial unique index prevents collisions. No frontmatter shim, no extra metadata file.
- Person merge and split are first-class operations through `vault.people.merge` / `vault.people.split` rather than SQL gymnastics. The `OR IGNORE` path on mention migration handles the case where a facet was linked to both rows pre-merge without race conditions.
- The MCP `resolve_person` tool returns the same shape the agent's UX layer needs (candidate list + is_exact flag), so a Claude Desktop conversation can render a quick-pick dialog when ambiguous.
- Importer scaffolding (`importers/_common.py`) generalises across vendors. ChatGPT and Claude land in v0.3; Obsidian / Notion / mbox would be one additional module each with no dispatcher rewrite.

**Negative:**

- People accumulate without garbage collection. A user who imports a ChatGPT export with 5 000 conversations could end up with hundreds of one-off person mentions. v0.3 ships no auto-prune; manual `tessera people merge` is the only consolidation path. Re-evaluate at v0.5 if real-user vaults grow unwieldy.
- Skill names are user-facing identifiers and must be unique per agent. A user who named two skills the same will hit `DuplicateSkillNameError` on the second `learn_skill` call. The error is loud; the v0.3 surface offers no auto-rename. Adding a `learn_skill_or_overwrite` variant is a v0.3.x enhancement, not a v0.3 commitment.
- Person-mention auto-extraction during import is documented future work. Without it, an imported conversation that mentions "Sarah" never auto-creates a person row — the user must invoke `resolve_person` interactively for each mention. This is a deliberate trade: shipping incomplete NER would create silent false-positive person rows the user can't undo.
- The default recall fan-out adds one more facet type (`skill`) to the SWCR topology. Per ADR 0011 the SWCR weights are co-occurrence-derived, not hardcoded, so new types contribute zero weight initially and gain weight as the user accumulates skill-tagged data. Steady-state behaviour is fine; transient cold-start behaviour for the first few skill captures is uncalibrated.

## Alternatives considered

- **People as facets, with the relationship graph encoded in metadata.** Rejected — content-hash dedup fights alias mutation, and `json_extract` joins on `metadata` are syntactically awkward compared to a real `people_id` foreign key.
- **Auto-pick resolution at the boundary.** Rejected for v0.3 — no calibration data, and a wrong auto-pick is hard to undo (the user has to find the bad mention link and unlink it).
- **Skills as a separate `skills` table, not facets.** Rejected — skills are content-bearing rows that benefit from FTS, vector embeddings, and the existing `recall` pipeline. Storing them as facets gets all of that for free; the only cost is a JSON metadata blob for `name`/`description`/`active`.
- **Person mentions auto-extracted via heuristic NER during import.** Rejected for v0.3 — see "Negative" above. The trade-off shifts when there is calibration data; revisit at v0.5 with vault contents to tune against.
- **`compiled_notebook` lifted into the default recall set at v0.3.** Rejected — the type is reserved for v0.5 write-time compilation. No rows exist; including it would create asymmetry between schema and pipeline. The one-line edit lifting it lands in the v0.5 commit that activates write-time compilation.

## References

- `docs/release-spec.md §v0.3` — the surface definition this ADR implements.
- `docs/adr/0010-five-facet-user-context-model.md` — the schema-level reservation of `person` / `skill` / `compiled_notebook` that v0.3 activates.
- `docs/adr/0011-swcr-default-on-cross-facet-coherence.md` — the cross-facet coherence primitive that the expanded recall fan-out exercises.
- `src/tessera/vault/people.py`, `src/tessera/vault/skills.py` — the implementation modules.
- `src/tessera/importers/_common.py`, `src/tessera/importers/chatgpt.py`, `src/tessera/importers/claude.py` — the importer surface.
