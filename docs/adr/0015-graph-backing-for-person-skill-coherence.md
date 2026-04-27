# ADR 0015 — Graph backing for person/skill coherence

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0011](0011-swcr-default-on-cross-facet-coherence.md), [ADR 0012](0012-v0-3-people-and-skills-design.md), `docs/swcr-spec.md`, `docs/benchmarks/B-RET-1-graph-backing-experiment/`

## Context

SWCR currently uses a pairwise entity-overlap β-term: two candidate facets receive coherence credit when their metadata entity sets overlap. ADR 0012 added people and skills to the current schema shape: people are rows in `people`, facet/person links live in `person_mentions`, and skills are `facet_type='skill'` rows with structured metadata. That creates a plausible graph structure beyond degree-1 entity overlap.

The architectural question is whether Tessera should retrofit graph backing for person/skill coherence now, before making any product claim that person/skill recall is graph-backed. Three options were considered:

1. **SQLite recursive CTE graph backing.** Keep one sqlite vault and derive graph neighborhoods from current schema tables plus deterministic metadata.
2. **Embedded graph database.** Add Kuzu or another embedded graph store beside the vault database.
3. **Keep the current Jaccard β-term.** Do not add graph backing until real data shows the current coherence signal is insufficient.

A side experiment was run against S1′, the person/skill B-RET-1 dataset in `docs/benchmarks/B-RET-1-swcr-ablation/dataset/s1_prime.json`.

## Decision

Keep the current entity-Jaccard β-term for now. Do not add production graph backing for person/skill coherence in the current schema.

This is Option C from the analysis plan: stay with Jaccard for now and revisit only if real user data or a future product claim requires graph-backed coherence.

## Graph model evaluated

The experiment evaluated a **typed entity graph**, not a facet-to-facet graph.

Nodes:

- `facet:<facet_id>`
- `person:<canonical_name>`
- `skill:<skill_name>`
- `entity:<metadata entity>`

Edges:

- bidirectional facet-person edges from S1′ `people` fields, loaded through the same conceptual shape as `person_mentions`
- bidirectional facet-skill edges from S1′ `skill_names`
- bidirectional facet-entity edges from S1′ `entities`

For each candidate facet, an experiment-only sqlite recursive CTE collected a deterministic degree-2 neighborhood. Pairwise Jaccard over those neighborhoods replaced only the SWCR β-term input. Production retrieval code was not modified.

The typed entity model was chosen over a facet-to-facet graph because it better matches ADR 0012 semantics. People are not facets, skills are facets, and future relationships such as `works_with`, `uses`, `derived_from`, and `expert_in` are naturally typed edges rather than anonymous facet co-mentions.

## Evidence

Experiment artifact:

- `docs/benchmarks/B-RET-1-graph-backing-experiment/run.py`
- `docs/benchmarks/B-RET-1-graph-backing-experiment/results/20260427T160009Z.json`

Dataset:

- S1′ person/skill dataset, 2,000 facets, 10 people, 25 bridge queries
- Query classes: `person_skill_bridge`, `ambiguous_person_skill_bridge`
- Deterministic fake hash embedder + keyword reranker proxy

Results:

| Variant | MRR@5 | nDCG@5 | Persona purity@5 | p95 latency |
|---|---:|---:|---:|---:|
| Current Jaccard β-term | 0.968 | 0.958 | 1.000 | 23.1 ms |
| SQLite CTE typed entity graph β-term | 0.973 | 0.960 | 1.000 | 71.2 ms |
| Delta | +0.005 | +0.002 | +0.000 | +48.1 ms |

The CTE graph variant produced a tiny automated quality gain, but the baseline was already near ceiling. The added latency was small in absolute terms but large in relative terms, and the experiment used an in-memory graph proxy rather than a production migration path.

The result is not strong enough to justify adding production graph schema, graph-maintenance code, migration work, and a new retrieval invariant now.

## Consequences

### Positive

- No production schema change is needed.
- No Kuzu or second on-disk graph store is introduced.
- Retrieval remains deterministic and simple: the β-term still comes from candidate metadata entity overlap.
- Tessera avoids making a graph-backed person/skill coherence claim before there is decisive evidence.

### Negative

- Person/skill coherence remains degree-1 overlap-based.
- The typed entity graph shape is not available to future features without a later ADR and migration.
- If real user vaults reveal entity-rich person/skill recall failures, this decision will need to be revisited.

### Follow-up

- Do not run the graph-backed coherence implementation work from the follow-up plan after this ADR. The decision is to keep Jaccard for now.
- Keep the S1′ dataset and graph-backing experiment directory as regression/decision artifacts.
- Revisit graph backing only with stronger evidence, preferably real-adapter S1′ runs plus real-user person/skill failure cases.

## Alternatives considered

### SQLite recursive CTE graph backing

Rejected for now. It is the cheapest graph-backed option and remains the default future hypothesis if graph backing becomes necessary, but the measured quality gain here was too small to justify production work.

### Embedded graph database

Rejected. Kuzu or another embedded graph database would add a second on-disk format, backup/export concerns, dependency surface, and migration complexity. The current experiment did not show enough quality pressure to justify even sqlite graph backing, so an embedded graph database is premature.

### Keep Jaccard

Accepted. Current behavior is simpler and already near ceiling on the S1′ fake-adapter proxy. This option preserves schema stability and avoids overclaiming graph-backed coherence.
