"""Synthetic dataset S1 generator for B-RET-1.

Produces a seed-controlled corpus of N facets across 5 personas, each
with a distinct voice, a consistent entity vocabulary, and a set of
goal themes. The output is a JSON file suitable for loading into a
fresh encrypted vault and running the ablation harness against.

Design:
    5 personas times 3 facet types times ~N/15 facets = N total.
    Each persona owns a set of entities (people, projects, tools).
    Facets are generated from templates that draw on persona-scoped
    entities and goal themes.
    Ground-truth queries pair an assume-identity-style prompt with the
    set of facets that belong to the target persona.

v0.1 facet types: episodic, semantic, style. Skills / relationships /
goals land in v0.3+ and are not generated here. The spec's 10K scale is
the P12 target; 2K is the P5 first-pass default because an in-session
ablation across four arms + 100 queries must complete in minutes, not
hours.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path


# Persona-scoped vocabulary. Each persona's entities rarely overlap with
# the others (2 shared ambient entities that mimic real cross-persona
# noise like "python" or "2026"). The ablation measures whether the
# retriever can surface persona-coherent facets despite ambient overlap.
@dataclass(frozen=True, slots=True)
class _Persona:
    id: str
    voice: str
    entities: tuple[str, ...]
    goals: tuple[str, ...]


_PERSONAS: list[_Persona] = [
    _Persona(
        id="tom_dev",
        voice="terse, imperative, code over prose",
        entities=("tessera", "sqlite", "ollama", "mcp", "rust"),
        goals=("ship v0.1", "reduce CI time", "document threat model"),
    ),
    _Persona(
        id="sarah_writer",
        voice="literary, first-person, reflective",
        entities=("manuscript", "editor_kim", "galley", "cover_design"),
        goals=("finish chapter seven", "respond to beta readers"),
    ),
    _Persona(
        id="alex_scientist",
        voice="precise, passive, citation-heavy",
        entities=("reagent_x", "lab_4", "prof_patel", "grant_nsf"),
        goals=("replicate 2025 result", "submit grant renewal"),
    ),
    _Persona(
        id="jordan_designer",
        voice="visual, second-person, action-oriented",
        entities=("figma_doc", "brand_palette", "client_atlas", "revision_7"),
        goals=("finalize rebrand", "prep presentation for atlas"),
    ),
    _Persona(
        id="morgan_analyst",
        voice="structured, data-first, sparse",
        entities=("q3_report", "ticker_acme", "spreadsheet_14", "cfo_chen"),
        goals=("close q3 memo", "update forecast model"),
    ),
]

_AMBIENT_ENTITIES: list[str] = ["python", "2026", "slack", "github"]

_EPISODIC_TEMPLATES = [
    "{persona_id} talked with {entity} about {goal} yesterday",
    "meeting notes on {goal} with {entity} at the standup",
    "decided to push {goal} after reviewing {entity}",
    "shipped work on {goal}, blocker cleared via {entity}",
    "chatted with {entity} re {goal} — captured action items",
]

_SEMANTIC_TEMPLATES = [
    "{entity} is the primary owner for {goal}",
    "{persona_id} prefers {entity} tooling when tackling {goal}",
    "fact: {entity} ties directly to {goal}",
    "the canonical path for {goal} routes through {entity}",
    "lesson learned: {goal} depends on {entity} staying healthy",
]

_STYLE_TEMPLATES = [
    "voice sample ({voice}): {persona_id} writes about {goal} and {entity}",
    "style note ({voice}): draft paragraph on {goal} mentioning {entity}",
    "writing sample, tone = {voice}: {entity} shows up as {goal}",
]

_QUERY_TEMPLATES = [
    "assume identity for {persona_id} working on {goal}",
    "recall {persona_id} notes about {goal}",
    "retrieve {persona_id} style and recent events for {goal}",
]


@dataclass(frozen=True, slots=True)
class Facet:
    facet_id: int
    persona: str
    facet_type: str
    content: str
    entities: list[str]
    captured_at: int


@dataclass(frozen=True, slots=True)
class Query:
    query_text: str
    persona: str
    relevant_facet_ids: list[int]


def generate(
    *,
    n_facets: int,
    n_queries: int,
    seed: int = 0,
) -> tuple[list[Facet], list[Query]]:
    rng = random.Random(seed)
    facets: list[Facet] = []
    facet_id = 1
    per_persona = n_facets // len(_PERSONAS)
    for persona in _PERSONAS:
        episodic_count = int(per_persona * 0.4)
        semantic_count = int(per_persona * 0.4)
        style_count = per_persona - episodic_count - semantic_count
        for facet_type, count, templates in (
            ("episodic", episodic_count, _EPISODIC_TEMPLATES),
            ("semantic", semantic_count, _SEMANTIC_TEMPLATES),
            ("style", style_count, _STYLE_TEMPLATES),
        ):
            facet_id = _emit_batch(
                facets,
                facet_id=facet_id,
                persona=persona,
                facet_type=facet_type,
                count=count,
                templates=templates,
                rng=rng,
            )
    queries = _build_queries(facets, n_queries=n_queries, rng=rng)
    return facets, queries


def _emit_batch(
    out: list[Facet],
    *,
    facet_id: int,
    persona: _Persona,
    facet_type: str,
    count: int,
    templates: list[str],
    rng: random.Random,
) -> int:
    for _ in range(count):
        tpl = rng.choice(templates)
        entity = rng.choice(persona.entities)
        goal = rng.choice(persona.goals)
        content = tpl.format(persona_id=persona.id, entity=entity, goal=goal, voice=persona.voice)
        ambient = [rng.choice(_AMBIENT_ENTITIES)] if rng.random() < 0.3 else []
        out.append(
            Facet(
                facet_id=facet_id,
                persona=persona.id,
                facet_type=facet_type,
                content=content,
                entities=[entity, *ambient],
                captured_at=1_000_000 + facet_id,
            )
        )
        facet_id += 1
    return facet_id


def _build_queries(facets: list[Facet], *, n_queries: int, rng: random.Random) -> list[Query]:
    per_persona_facets: dict[str, list[int]] = {}
    for facet in facets:
        per_persona_facets.setdefault(facet.persona, []).append(facet.facet_id)
    per_persona = max(1, n_queries // len(_PERSONAS))
    queries: list[Query] = []
    for persona in _PERSONAS:
        for _ in range(per_persona):
            goal = rng.choice(persona.goals)
            tpl = rng.choice(_QUERY_TEMPLATES)
            queries.append(
                Query(
                    query_text=tpl.format(persona_id=persona.id, goal=goal),
                    persona=persona.id,
                    relevant_facet_ids=list(per_persona_facets[persona.id]),
                )
            )
    return queries


def write_json(path: Path, facets: list[Facet], queries: list[Query]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "facets": [
            {
                "facet_id": f.facet_id,
                "persona": f.persona,
                "facet_type": f.facet_type,
                "content": f.content,
                "entities": f.entities,
                "captured_at": f.captured_at,
            }
            for f in facets
        ],
        "queries": [
            {
                "query_text": q.query_text,
                "persona": q.persona,
                "relevant_facet_ids": q.relevant_facet_ids,
            }
            for q in queries
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate-s1")
    parser.add_argument("--n-facets", type=int, default=2000)
    parser.add_argument("--n-queries", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "s1.json",
    )
    args = parser.parse_args(argv)
    facets, queries = generate(n_facets=args.n_facets, n_queries=args.n_queries, seed=args.seed)
    write_json(args.out, facets, queries)
    print(f"wrote {len(facets)} facets and {len(queries)} queries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
