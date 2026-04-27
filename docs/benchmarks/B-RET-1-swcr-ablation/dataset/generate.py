"""Synthetic dataset generator for B-RET-1.

Produces seed-controlled corpora suitable for loading into a fresh encrypted
vault and running the SWCR ablation harness against.

Variants:
    s1: original v0.1 five-facet persona dataset.
    s1-prime: harder person/skill dataset for graph-backing experiments.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    people: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Query:
    query_text: str
    persona: str
    relevant_facet_ids: list[int]
    query_class: str = "persona_recall"
    expected_people: tuple[str, ...] = ()
    expected_skills: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonEntry:
    person_id: int
    canonical_name: str
    aliases: tuple[str, ...]
    persona: str


@dataclass(frozen=True, slots=True)
class PrimePayload:
    facets: list[Facet]
    queries: list[Query]
    people: list[PersonEntry]


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


@dataclass(frozen=True, slots=True)
class _PrimeSkill:
    name: str
    description: str
    anchor_entity: str


@dataclass(frozen=True, slots=True)
class _PrimePersona:
    id: str
    voice: str
    role: str
    entities: tuple[str, ...]
    projects: tuple[str, ...]
    collaborators: tuple[str, str]
    skills: tuple[_PrimeSkill, _PrimeSkill]


_PRIME_PERSONAS: tuple[_PrimePersona, ...] = (
    _PrimePersona(
        id="tom_dev",
        voice="terse, imperative, code over prose",
        role="backend engineer shipping Tessera retrieval",
        entities=("sqlite", "swcr", "fastembed", "mcp", "audit_log"),
        projects=("graph backed recall", "release hardening"),
        collaborators=("Maya Chen", "Riley Ortiz"),
        skills=(
            _PrimeSkill("api latency triage", "separate embedding, SQL, and rerank latency", "swcr"),
            _PrimeSkill("vault migration review", "verify forward-only schema changes", "sqlite"),
        ),
    ),
    _PrimePersona(
        id="sarah_writer",
        voice="literary, first-person, reflective",
        role="novelist coordinating edits",
        entities=("manuscript", "galley", "copyedit", "chapter_arc", "voice_sheet"),
        projects=("chapter seven rewrite", "beta reader response"),
        collaborators=("Maya Patel", "Jon Reeves"),
        skills=(
            _PrimeSkill("scene tension pass", "raise conflict without changing plot beats", "chapter_arc"),
            _PrimeSkill("editorial response synthesis", "group feedback into revision moves", "copyedit"),
        ),
    ),
    _PrimePersona(
        id="alex_scientist",
        voice="precise, passive, citation-heavy",
        role="wet-lab researcher managing replication work",
        entities=("reagent_x", "lab_4", "assay_queue", "grant_nsf", "protocol_delta"),
        projects=("replicate 2025 result", "grant renewal"),
        collaborators=("Priya Shah", "Riley Okafor"),
        skills=(
            _PrimeSkill("assay anomaly review", "separate batch effects from protocol drift", "assay_queue"),
            _PrimeSkill("grant evidence mapping", "tie experiment outcomes to proposal claims", "grant_nsf"),
        ),
    ),
    _PrimePersona(
        id="jordan_designer",
        voice="visual, second-person, action-oriented",
        role="product designer handling rebrands",
        entities=("figma_doc", "brand_palette", "client_atlas", "revision_7", "handoff_grid"),
        projects=("atlas rebrand", "design system handoff"),
        collaborators=("Maya Stone", "Leo Kim"),
        skills=(
            _PrimeSkill("brand critique framing", "turn subjective feedback into visual criteria", "brand_palette"),
            _PrimeSkill("handoff checklist", "prepare developer-ready component notes", "handoff_grid"),
        ),
    ),
    _PrimePersona(
        id="morgan_analyst",
        voice="structured, data-first, sparse",
        role="financial analyst owning forecasts",
        entities=("q3_report", "ticker_acme", "spreadsheet_14", "cfo_chen", "variance_bridge"),
        projects=("quarterly forecast", "board memo"),
        collaborators=("Nora Singh", "Leo Park"),
        skills=(
            _PrimeSkill("variance bridge", "explain forecast movement by driver", "variance_bridge"),
            _PrimeSkill("board memo compression", "reduce model output to executive bullets", "q3_report"),
        ),
    ),
)

_PRIME_FACET_MIX: tuple[tuple[str, float], ...] = (
    ("identity", 0.05),
    ("preference", 0.12),
    ("workflow", 0.18),
    ("project", 0.25),
    ("style", 0.25),
    ("skill", 0.15),
)


def generate_s1_prime(
    *,
    n_facets: int,
    n_queries: int,
    seed: int = 0,
) -> PrimePayload:
    """Generate S1′, a harder person/skill retrieval dataset.

    S1′ keeps the five original persona buckets but adds v0.3 semantics:
    skill facets, explicit people rows, and person-to-facet mention links.
    Query text intentionally mixes shared first names (three Mayas, two
    Rileys, two Leos) with skill names so graph-backing experiments can
    test whether person/skill neighborhoods improve bundle coherence over
    isolated keyword or embedding matches.
    """

    rng = random.Random(seed)
    facets: list[Facet] = []
    people = _prime_people()
    facet_id = 1
    per_persona = max(len(_PRIME_FACET_MIX), n_facets // len(_PRIME_PERSONAS))
    for persona in _PRIME_PERSONAS:
        allocated = 0
        for index, (facet_type, fraction) in enumerate(_PRIME_FACET_MIX):
            if index == len(_PRIME_FACET_MIX) - 1:
                count = per_persona - allocated
            else:
                count = max(1, int(per_persona * fraction))
                allocated += count
            facet_id = _emit_prime_batch(
                facets,
                facet_id=facet_id,
                persona=persona,
                facet_type=facet_type,
                count=count,
                rng=rng,
            )
    queries = _build_prime_queries(facets, n_queries=n_queries, rng=rng)
    return PrimePayload(facets=facets, queries=queries, people=people)


def _prime_people() -> list[PersonEntry]:
    people: list[PersonEntry] = []
    person_id = 1
    for persona in _PRIME_PERSONAS:
        for name in persona.collaborators:
            people.append(
                PersonEntry(
                    person_id=person_id,
                    canonical_name=name,
                    aliases=(name.split()[0],),
                    persona=persona.id,
                )
            )
            person_id += 1
    return people


def _emit_prime_batch(
    out: list[Facet],
    *,
    facet_id: int,
    persona: _PrimePersona,
    facet_type: str,
    count: int,
    rng: random.Random,
) -> int:
    for local_index in range(count):
        collaborator = rng.choice(persona.collaborators)
        skill = rng.choice(persona.skills)
        project = rng.choice(persona.projects)
        entity = rng.choice((*persona.entities, skill.anchor_entity))
        content = _prime_content(
            persona=persona,
            facet_type=facet_type,
            collaborator=collaborator,
            skill=skill,
            project=project,
            entity=entity,
            local_index=local_index,
        )
        out.append(
            Facet(
                facet_id=facet_id,
                persona=persona.id,
                facet_type=facet_type,
                content=content,
                entities=sorted({entity, skill.anchor_entity}),
                people=(collaborator,),
                skill_names=(skill.name,),
                captured_at=2_000_000 + facet_id,
            )
        )
        facet_id += 1
    return facet_id


def _prime_content(
    *,
    persona: _PrimePersona,
    facet_type: str,
    collaborator: str,
    skill: _PrimeSkill,
    project: str,
    entity: str,
    local_index: int,
) -> str:
    if facet_type == "identity":
        return (
            f"{persona.id} is a {persona.role}; works with {collaborator} on {project}; "
            f"person-skill note {local_index} anchors {skill.name} via {entity}"
        )
    if facet_type == "preference":
        return (
            f"{persona.id} prefers {skill.name} updates for {collaborator} to start with "
            f"the {entity} constraint before prose; note {local_index}"
        )
    if facet_type == "workflow":
        return (
            f"workflow for {project}: ask {collaborator} for the missing edge case, run "
            f"{skill.name}, then summarize decisions around {entity}; step note {local_index}"
        )
    if facet_type == "project":
        return (
            f"project state {project}: {collaborator} is blocked on {entity}; "
            f"{persona.id} will use {skill.name} to unblock the next review; note {local_index}"
        )
    if facet_type == "style":
        return (
            f"voice sample ({persona.voice}) for {collaborator}: explain {project} through "
            f"{skill.name} and {entity}; style shard {local_index}"
        )
    if facet_type == "skill":
        return (
            f"skill: {skill.name}. Procedure for {persona.id} with {collaborator}: "
            f"{skill.description}; inspect {entity}, collect project evidence for {project}, "
            f"write the smallest next action. skill shard {local_index}"
        )
    raise ValueError(f"unsupported S1′ facet_type: {facet_type}")


def _build_prime_queries(
    facets: list[Facet], *, n_queries: int, rng: random.Random
) -> list[Query]:
    queries: list[Query] = []
    for persona in _PRIME_PERSONAS:
        persona_facets = [f for f in facets if f.persona == persona.id]
        for collaborator in persona.collaborators:
            for skill in persona.skills:
                relevant = [
                    f.facet_id
                    for f in persona_facets
                    if collaborator in f.people and skill.name in f.skill_names
                ]
                project = rng.choice(persona.projects)
                queries.append(
                    Query(
                        query_text=(
                            f"recall {persona.id} context for {collaborator} using "
                            f"{skill.name} on {project}"
                        ),
                        persona=persona.id,
                        relevant_facet_ids=relevant,
                        query_class="person_skill_bridge",
                        expected_people=(collaborator,),
                        expected_skills=(skill.name,),
                    )
                )
    # Add duplicate-shaped prompts with first-name ambiguity so graph variants
    # can use canonical people links instead of raw token overlap alone.
    for persona in _PRIME_PERSONAS:
        collaborator = persona.collaborators[0]
        first_name = collaborator.split()[0]
        skill = persona.skills[0]
        relevant = [
            f.facet_id
            for f in facets
            if f.persona == persona.id
            and collaborator in f.people
            and skill.name in f.skill_names
        ]
        queries.append(
            Query(
                query_text=f"what should I remember about {first_name} and {skill.name}?",
                persona=persona.id,
                relevant_facet_ids=relevant,
                query_class="ambiguous_person_skill_bridge",
                expected_people=(collaborator,),
                expected_skills=(skill.name,),
            )
        )
    rng.shuffle(queries)
    return queries[:n_queries]


def write_json(
    path: Path,
    facets: list[Facet],
    queries: list[Query],
    *,
    people: list[PersonEntry] | None = None,
    dataset_variant: str = "s1",
    description: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "dataset_variant": dataset_variant,
        "facets": [
            {
                "facet_id": f.facet_id,
                "persona": f.persona,
                "facet_type": f.facet_type,
                "content": f.content,
                "entities": f.entities,
                "people": list(f.people),
                "skill_names": list(f.skill_names),
                "captured_at": f.captured_at,
            }
            for f in facets
        ],
        "people": [
            {
                "person_id": p.person_id,
                "canonical_name": p.canonical_name,
                "aliases": list(p.aliases),
                "persona": p.persona,
            }
            for p in (people or [])
        ],
        "queries": [
            {
                "query_text": q.query_text,
                "persona": q.persona,
                "relevant_facet_ids": q.relevant_facet_ids,
                "query_class": q.query_class,
                "expected_people": list(q.expected_people),
                "expected_skills": list(q.expected_skills),
            }
            for q in queries
        ],
    }
    if description is not None:
        payload["description"] = description
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate-s1")
    parser.add_argument("--variant", choices=("s1", "s1-prime"), default="s1")
    parser.add_argument("--n-facets", type=int, default=2000)
    parser.add_argument("--n-queries", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out = args.out or Path(__file__).parent / (
        "s1_prime.json" if args.variant == "s1-prime" else "s1.json"
    )
    if args.variant == "s1-prime":
        payload = generate_s1_prime(
            n_facets=args.n_facets,
            n_queries=args.n_queries,
            seed=args.seed,
        )
        write_json(
            out,
            payload.facets,
            payload.queries,
            people=payload.people,
            dataset_variant="s1_prime_person_skill",
            description=(
                "Harder B-RET-1 dataset with skill facets, people rows, "
                "person_mentions links, and person/skill bridge queries."
            ),
        )
        print(
            f"wrote {len(payload.facets)} facets, {len(payload.people)} people, "
            f"and {len(payload.queries)} queries to {out}"
        )
    else:
        facets, queries = generate(n_facets=args.n_facets, n_queries=args.n_queries, seed=args.seed)
        write_json(out, facets, queries)
        print(f"wrote {len(facets)} facets and {len(queries)} queries to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
