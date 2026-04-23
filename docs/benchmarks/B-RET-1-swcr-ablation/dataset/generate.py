"""Synthetic dataset S1 generator for B-RET-1.

Produces a seed-controlled corpus of N facets across 5 personas, each
with a distinct voice, a consistent entity vocabulary, and a set of
project themes. The output is a JSON file suitable for loading into
a fresh encrypted vault and running the ablation harness against.

Design:
    5 personas times 5 facet types times ~N/25 facets = N total.
    Each persona owns a set of entities (people, projects, tools).
    Facets are generated from templates that draw on persona-scoped
    entities and project themes.
    Ground-truth queries pair a cross-facet ``recall`` prompt with
    the set of facets that belong to the target persona.

v0.1 facet types: identity, preference, workflow, project, style. The
reserved v0.3+ types (person, skill, compiled_notebook) land in later
versions and are not generated here. The 10K scale is the bench
finalisation target; 2_000 is the default because an in-session
ablation across three arms and 50 queries must complete in minutes,
not hours.
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
    role: str
    entities: tuple[str, ...]
    projects: tuple[str, ...]


_PERSONAS: list[_Persona] = [
    _Persona(
        id="tom_dev",
        voice="terse, imperative, code over prose",
        role="backend engineer shipping an AI-tools portable context layer",
        entities=("tessera", "sqlite", "ollama", "mcp", "rust"),
        projects=("ship v0.1", "reduce CI time", "document threat model"),
    ),
    _Persona(
        id="sarah_writer",
        voice="literary, first-person, reflective",
        role="novelist drafting and editing long-form fiction",
        entities=("manuscript", "editor_kim", "galley", "cover_design"),
        projects=("finish chapter seven", "respond to beta readers"),
    ),
    _Persona(
        id="alex_scientist",
        voice="precise, passive, citation-heavy",
        role="research scientist running a wet lab",
        entities=("reagent_x", "lab_4", "prof_patel", "grant_nsf"),
        projects=("replicate 2025 result", "submit grant renewal"),
    ),
    _Persona(
        id="jordan_designer",
        voice="visual, second-person, action-oriented",
        role="product designer handling client rebrands",
        entities=("figma_doc", "brand_palette", "client_atlas", "revision_7"),
        projects=("finalize rebrand", "prep presentation for atlas"),
    ),
    _Persona(
        id="morgan_analyst",
        voice="structured, data-first, sparse",
        role="financial analyst owning the quarterly forecast",
        entities=("q3_report", "ticker_acme", "spreadsheet_14", "cfo_chen"),
        projects=("close q3 memo", "update forecast model"),
    ),
]

_AMBIENT_ENTITIES: list[str] = ["python", "2026", "slack", "github"]

_IDENTITY_TEMPLATES = [
    "{persona_id} is a {role}",
    "{persona_id} works primarily out of {entity} and identifies as a {role}",
    "stable fact about {persona_id}: role = {role}",
]

_PREFERENCE_TEMPLATES = [
    "{persona_id} prefers {entity} over alternatives when tackling {project}",
    "{persona_id} always uses {entity} for {project}",
    "rule: when shipping {project}, use {entity} first",
    "behavioural rule — {persona_id} keeps {entity} as the default for {project}",
]

_WORKFLOW_TEMPLATES = [
    "workflow: for {project}, {persona_id} starts with {entity} then iterates",
    "procedure — break {project} into five beats, anchored on {entity}",
    "standard flow for {project}: gather {entity}, draft, review, ship",
    "{persona_id}'s repeatable shape for {project} routes through {entity}",
]

_PROJECT_TEMPLATES = [
    "active work on {project}: unblocked via {entity}",
    "project note — {project} is the current focus, touched {entity} today",
    "what {persona_id} is building: {project}, blocker tracked under {entity}",
    "in-flight: {project}, owner {persona_id}, adjacent to {entity}",
]

_STYLE_TEMPLATES = [
    "voice sample ({voice}): {persona_id} writes about {project} and {entity}",
    "style note ({voice}): draft paragraph on {project} mentioning {entity}",
    "writing sample, tone = {voice}: {entity} shows up as {project}",
]

_QUERY_TEMPLATES = [
    "draft an update for {persona_id} working on {project}",
    "recall {persona_id} context about {project}",
    "retrieve {persona_id} voice, workflow, and current state on {project}",
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


# Per-persona facet-type mix. Identity is the rarest (stable-for-years
# facts, one or two per persona); style is the largest (voice samples);
# preference / workflow / project fill out the middle. Fractions sum to
# 1.0 and are applied to each persona's share of ``n_facets``.
_FACET_MIX: tuple[tuple[str, float, list[str]], ...] = (
    ("identity", 0.05, _IDENTITY_TEMPLATES),
    ("preference", 0.15, _PREFERENCE_TEMPLATES),
    ("workflow", 0.15, _WORKFLOW_TEMPLATES),
    ("project", 0.30, _PROJECT_TEMPLATES),
    ("style", 0.35, _STYLE_TEMPLATES),
)


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
        allocated = 0
        # All but the last entry get their integer share; the last
        # absorbs the remainder so per_persona is hit exactly.
        for index, (facet_type, fraction, templates) in enumerate(_FACET_MIX):
            if index == len(_FACET_MIX) - 1:
                count = per_persona - allocated
            else:
                count = max(1, int(per_persona * fraction))
                allocated += count
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
        project = rng.choice(persona.projects)
        content = tpl.format(
            persona_id=persona.id,
            entity=entity,
            project=project,
            voice=persona.voice,
            role=persona.role,
        )
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
            project = rng.choice(persona.projects)
            tpl = rng.choice(_QUERY_TEMPLATES)
            queries.append(
                Query(
                    query_text=tpl.format(persona_id=persona.id, project=project),
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
