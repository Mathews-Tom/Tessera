# B-RET-1 — graph-backing experiment for person/skill coherence

This side experiment evaluates whether replacing SWCR's current entity-Jaccard β-term with a sqlite recursive-CTE graph-neighborhood β-term improves person/skill coherence on S1′.

The experiment is intentionally separate from production retrieval code. It reads the committed S1′ dataset from `../B-RET-1-swcr-ablation/dataset/s1_prime.json`, builds an in-memory sqlite typed entity graph, and compares two configurations with the same deterministic fake adapter proxy:

| Config | β-term source | Production impact |
|---|---|---|
| `baseline_jaccard` | Current pairwise Jaccard over facet `entities` metadata | None, mirrors current SWCR β-term shape |
| `sqlite_cte_typed_entity` | Degree-2 recursive-CTE neighborhood overlap over facet/person/skill/entity nodes | Experiment-only |

## Graph model under test

The tested model is a typed entity graph:

- facet nodes: `facet:<facet_id>`
- person nodes: `person:<canonical_name>`
- skill nodes: `skill:<skill_name>`
- entity nodes: `entity:<metadata entity>`
- bidirectional edges from each facet to every person, skill, and metadata entity it references

For each candidate facet, the runner uses sqlite recursive CTEs to collect a deterministic degree-2 neighborhood, then computes pairwise Jaccard overlap between candidate neighborhoods. This replaces only the β-term input; the rest of the SWCR scoring formula stays fixed for the experiment.

## Reproduce

```bash
uv run python docs/benchmarks/B-RET-1-graph-backing-experiment/run.py
```

Results land under `results/<utc-timestamp>.json`. The runner refuses to overwrite an existing result file.

## Interpretation limits

- Uses deterministic fake embeddings/reranking so it is offline and repeatable.
- Measures automated MRR@5, nDCG@5, persona-purity@5, and per-query scoring latency.
- Does not claim production retrieval quality. The goal is to decide whether graph backing is promising enough to justify a schema/retrieval change.
