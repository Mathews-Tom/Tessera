from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, cast

DATASET = Path("docs/benchmarks/B-RET-1-swcr-ablation/dataset/s1_prime.json")


def _load() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(DATASET.read_text()))


def test_s1_prime_dataset_exists_with_person_skill_variant() -> None:
    data = _load()

    assert data["dataset_variant"] == "s1_prime_person_skill"
    assert data["facets"]
    assert data["people"]
    assert data["queries"]


def test_s1_prime_contains_skill_facets_and_person_mentions() -> None:
    data = _load()

    facet_types = Counter(f["facet_type"] for f in data["facets"])
    assert facet_types["skill"] > 0
    assert {"identity", "preference", "workflow", "project", "style", "skill"}.issubset(facet_types)

    people_by_name = {p["canonical_name"] for p in data["people"]}
    mentioned_people = {person for f in data["facets"] for person in f.get("people", [])}
    assert mentioned_people
    assert mentioned_people.issubset(people_by_name)

    skill_names = {skill for f in data["facets"] for skill in f.get("skill_names", [])}
    assert skill_names


def test_s1_prime_has_person_skill_bridge_queries_with_ground_truth() -> None:
    data = _load()
    facet_ids = {f["facet_id"] for f in data["facets"]}
    people_by_name = {p["canonical_name"] for p in data["people"]}
    skill_names = {skill for f in data["facets"] for skill in f.get("skill_names", [])}

    bridge_queries = [
        q
        for q in data["queries"]
        if q.get("query_class") in {"person_skill_bridge", "ambiguous_person_skill_bridge"}
    ]
    assert bridge_queries

    for query in bridge_queries:
        assert query["relevant_facet_ids"]
        assert set(query["relevant_facet_ids"]).issubset(facet_ids)
        assert query["expected_people"]
        assert set(query["expected_people"]).issubset(people_by_name)
        assert query["expected_skills"]
        assert set(query["expected_skills"]).issubset(skill_names)
