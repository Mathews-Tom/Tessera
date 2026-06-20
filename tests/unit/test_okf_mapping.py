"""OKF facet <-> concept mapping — pure, I/O-free (ADR 0023, plan Phase 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tessera.vault import okf
from tessera.vault.facets import ALL_FACET_TYPES
from tessera.vault.okf import ConceptFacet

_ULID = "01JY8Z3K7Q9V2N4R6T8W0X1Y2Z"


def _facet(
    *,
    facet_type: str = "preference",
    external_id: str = _ULID,
    content_hash: str = "a" * 64,
    captured_at: int = 1_716_900_000,
    mode: str = "query_time",
    volatility: str = "persistent",
    ttl_seconds: int | None = None,
    is_stale: bool | None = None,
) -> ConceptFacet:
    return ConceptFacet(
        external_id=external_id,
        facet_type=facet_type,
        content_hash=content_hash,
        captured_at=captured_at,
        mode=mode,
        volatility=volatility,
        ttl_seconds=ttl_seconds,
        is_stale=is_stale,
    )


# --------------------------------------------------------------------------
# Conformance: every facet type yields a non-empty `type` + the ULID.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("facet_type", sorted(ALL_FACET_TYPES))
def test_every_facet_type_yields_conformant_frontmatter(facet_type: str) -> None:
    fm = okf.facet_to_frontmatter(_facet(facet_type=facet_type), {})
    assert isinstance(fm["type"], str)
    assert fm["type"].strip(), "SPEC §9 requires a non-empty type"
    assert fm[okf.TESSERA_EXTERNAL_ID] == _ULID
    assert fm[okf.TESSERA_FACET_TYPE] == facet_type


@pytest.mark.unit
def test_display_type_is_title_cased() -> None:
    fm = okf.facet_to_frontmatter(_facet(facet_type="compiled_notebook"), {})
    assert fm["type"] == "Compiled Notebook"
    assert fm[okf.TESSERA_FACET_TYPE] == "compiled_notebook"


# --------------------------------------------------------------------------
# Volatility / TTL / is_stale emission rules.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_persistent_rows_omit_volatility_key() -> None:
    fm = okf.facet_to_frontmatter(_facet(volatility="persistent"), {})
    assert okf.TESSERA_VOLATILITY not in fm
    assert okf.TESSERA_TTL_SECONDS not in fm


@pytest.mark.unit
def test_non_persistent_rows_carry_volatility_and_ttl() -> None:
    fm = okf.facet_to_frontmatter(_facet(volatility="session", ttl_seconds=3600), {})
    assert fm[okf.TESSERA_VOLATILITY] == "session"
    assert fm[okf.TESSERA_TTL_SECONDS] == 3600


@pytest.mark.unit
def test_is_stale_present_only_when_set() -> None:
    assert okf.TESSERA_IS_STALE not in okf.facet_to_frontmatter(_facet(), {})
    fm = okf.facet_to_frontmatter(_facet(facet_type="compiled_notebook", is_stale=False), {})
    assert fm[okf.TESSERA_IS_STALE] is False


# --------------------------------------------------------------------------
# Metadata projection.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_metadata_maps_to_soft_standard_fields() -> None:
    metadata = {
        "name": "Package manager preference",
        "description": "Prefer uv.",
        "tags": ["python", "tooling"],
        "resource": "https://example.com/x",
    }
    fm = okf.facet_to_frontmatter(_facet(), metadata)
    assert fm["title"] == "Package manager preference"
    assert fm["description"] == "Prefer uv."
    assert fm["tags"] == ["python", "tooling"]
    assert fm["resource"] == "https://example.com/x"


@pytest.mark.unit
def test_missing_metadata_omits_optional_fields() -> None:
    fm = okf.facet_to_frontmatter(_facet(), {})
    for key in ("title", "description", "tags", "resource"):
        assert key not in fm


@pytest.mark.unit
def test_timestamp_uses_canonical_iso_format() -> None:
    fm = okf.facet_to_frontmatter(_facet(captured_at=0), {})
    assert fm["timestamp"] == "1970-01-01T00:00:00.000000Z"


# --------------------------------------------------------------------------
# Round-trip: facet_to_frontmatter -> frontmatter_to_facet_fields.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_round_trip_preserves_identity_type_and_mode() -> None:
    facet = _facet(facet_type="compiled_notebook", mode="write_time", is_stale=False)
    fm = okf.facet_to_frontmatter(facet, {"name": "SWCR design brief"})
    recovered = okf.frontmatter_to_facet_fields(fm)
    assert recovered.external_id == _ULID
    assert recovered.facet_type == "compiled_notebook"
    assert recovered.mode == "write_time"


@pytest.mark.unit
def test_facet_type_prefers_extension_key_over_display() -> None:
    fm: dict[str, Any] = {
        "type": "Totally Different",
        okf.TESSERA_FACET_TYPE: "compiled_notebook",
        okf.TESSERA_MODE: "write_time",
    }
    recovered = okf.frontmatter_to_facet_fields(fm)
    assert recovered.facet_type == "compiled_notebook"


@pytest.mark.unit
def test_facet_type_falls_back_to_display_when_extension_absent() -> None:
    recovered = okf.frontmatter_to_facet_fields({"type": "Compiled Notebook"})
    assert recovered.facet_type == "compiled_notebook"


@pytest.mark.unit
def test_mode_defaults_to_query_time_when_absent() -> None:
    recovered = okf.frontmatter_to_facet_fields({"type": "Preference"})
    assert recovered.mode == "query_time"


# --------------------------------------------------------------------------
# Idempotency: emitted frontmatter parses back identically.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("facet_type", sorted(ALL_FACET_TYPES))
def test_frontmatter_render_parse_is_idempotent(facet_type: str) -> None:
    facet = _facet(
        facet_type=facet_type,
        mode="write_time",
        volatility="session",
        ttl_seconds=3600,
        is_stale=True,
    )
    metadata = {
        "name": "Example",
        "description": 'An example facet: with a colon and "quotes".',
        "tags": ["alpha", "beta"],
    }
    fm = okf.facet_to_frontmatter(facet, metadata)
    parsed = okf.parse_concept(okf.render_frontmatter(fm))
    assert parsed.frontmatter == fm
    assert parsed.body == ""


@pytest.mark.unit
def test_render_concept_round_trips_frontmatter_body_and_citations() -> None:
    facet = _facet(facet_type="compiled_notebook", mode="write_time", is_stale=False)
    metadata = {"name": "SWCR design brief", "description": "Brief."}
    body = "# Purpose\n\nRendered narrative."
    citations = ["[1] [project: anneal](/project/anneal.md)"]
    text = okf.render_concept(facet, metadata, body, citations)
    parsed = okf.parse_concept(text)
    assert parsed.frontmatter == okf.facet_to_frontmatter(facet, metadata)
    assert "# Purpose" in parsed.body
    assert "# Citations" in parsed.body
    assert "/project/anneal.md" in parsed.body


@pytest.mark.unit
def test_render_concept_without_citations_omits_section() -> None:
    text = okf.render_concept(_facet(), {"name": "X"}, "Body only.")
    assert "# Citations" not in text


@pytest.mark.unit
def test_render_frontmatter_orders_required_block_then_sorted_keys() -> None:
    fm = okf.facet_to_frontmatter(_facet(mode="write_time"), {"name": "X", "tags": ["b", "a"]})
    keys = [
        line.split(":", 1)[0]
        for line in okf.render_frontmatter(fm).splitlines()
        if line != "---" and ":" in line
    ]
    assert keys[0] == "type"
    tessera_keys = [k for k in keys if k.startswith("tessera_")]
    assert tessera_keys == sorted(tessera_keys)
    last_required = max(i for i, k in enumerate(keys) if not k.startswith("tessera_"))
    first_tessera = min(i for i, k in enumerate(keys) if k.startswith("tessera_"))
    assert first_tessera > last_required


# --------------------------------------------------------------------------
# Extension tolerance: unknown keys survive a parse -> render round-trip.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_keys_survive_parse_render_round_trip() -> None:
    text = (
        "---\n"
        'type: "Preference"\n'
        f'{okf.TESSERA_EXTERNAL_ID}: "{_ULID}"\n'
        f'{okf.TESSERA_FACET_TYPE}: "preference"\n'
        'custom_key: "some value"\n'
        "another_unknown: 42\n"
        "---\n\nBody.\n"
    )
    parsed = okf.parse_concept(text)
    assert parsed.frontmatter["custom_key"] == "some value"
    assert parsed.frontmatter["another_unknown"] == 42

    rerendered = okf.render_frontmatter(parsed.frontmatter)
    reparsed = okf.parse_concept(rerendered)
    assert reparsed.frontmatter == parsed.frontmatter


@pytest.mark.unit
def test_unknown_keys_land_in_extra() -> None:
    fm: dict[str, Any] = {
        "type": "Preference",
        "custom_key": "x",
        okf.TESSERA_EXTERNAL_ID: _ULID,
    }
    recovered = okf.frontmatter_to_facet_fields(fm)
    assert recovered.extra == {"custom_key": "x"}


# --------------------------------------------------------------------------
# parse_concept tolerance (SPEC §9) vs. strict-on-malformed.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_document_without_frontmatter_is_body_only() -> None:
    text = "# Just a heading\n\nNo frontmatter here.\n"
    parsed = okf.parse_concept(text)
    assert parsed.frontmatter == {}
    assert parsed.body == text


@pytest.mark.unit
def test_unclosed_frontmatter_fence_raises() -> None:
    with pytest.raises(okf.OKFParseError):
        okf.parse_concept("---\ntype: Preference\nno closing fence\n")


@pytest.mark.unit
def test_non_key_value_frontmatter_line_raises() -> None:
    with pytest.raises(okf.OKFParseError):
        okf.parse_concept("---\nthis is not a pair\n---\nbody\n")


@pytest.mark.unit
def test_tolerant_scalar_parsing_of_foreign_frontmatter() -> None:
    text = (
        "---\n"
        "type: Preference\n"
        "tags: [python, tooling]\n"
        "tessera_is_stale: false\n"
        "tessera_ttl_seconds: 3600\n"
        "empty_value:\n"
        "---\nbody\n"
    )
    fm = okf.parse_concept(text).frontmatter
    assert fm["type"] == "Preference"
    assert fm["tags"] == ["python", "tooling"]
    assert fm["tessera_is_stale"] is False
    assert fm["tessera_ttl_seconds"] == 3600
    assert fm["empty_value"] is None


@pytest.mark.unit
def test_block_list_frontmatter_parses() -> None:
    text = "---\ntype: Preference\ntags:\n  - python\n  - tooling\n---\nbody\n"
    fm = okf.parse_concept(text).frontmatter
    assert fm["tags"] == ["python", "tooling"]


# --------------------------------------------------------------------------
# concept_id / concept_slug.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_concept_id_joins_type_and_slug() -> None:
    assert okf.concept_id("preference", "uv-over-pip") == "preference/uv-over-pip"


@pytest.mark.unit
def test_concept_slug_reuses_slugify() -> None:
    assert okf.concept_slug("UV over pip!", _ULID) == "uv-over-pip"


@pytest.mark.unit
def test_concept_slug_resolves_collision_with_ulid_suffix() -> None:
    taken = {"uv-over-pip"}
    slug = okf.concept_slug("UV over pip", _ULID, taken)
    assert slug == "uv-over-pip-" + _ULID[-6:].lower()
    assert slug not in taken


@pytest.mark.unit
def test_concept_slug_falls_back_to_ulid_when_name_unslugable() -> None:
    assert okf.concept_slug("!!!", _ULID) == _ULID.lower()


# --------------------------------------------------------------------------
# Purity guard: the module performs no filesystem or DB I/O.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_module_stays_io_free() -> None:
    source = Path(okf.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import sqlcipher3",
        "import sqlite3",
        "from pathlib import",
        "from tessera.vault.connection",
        ".execute(",
    )
    offenders = [token for token in forbidden if token in source]
    assert not offenders, f"okf.py must stay I/O-free; found {offenders}"
