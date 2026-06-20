"""OKF (Open Knowledge Format) facet <-> concept mapping — pure and I/O-free.

Phase 1 of the OKF interchange boundary (ADR 0023): the facet <-> concept
mapping expressed as pure functions with no filesystem and no database
access. Keeping the mapping I/O-free makes it unit-testable in isolation
and lets the eventual exporter / importer (enhancement-plan Phases 2 & 4)
be a thin I/O shell over these functions.

Boundary (ADR 0023): *Tessera stores encrypted; an OKF bundle is what the
user explicitly asks Tessera to emit.* This module only translates between
a vault facet and one OKF concept document (YAML frontmatter + markdown
body); it never reads or writes the vault or the disk.

Design notes
------------
- The projection input is :class:`ConceptFacet`, a decoupled subset of a
  vault facet row. It is deliberately **not** ``facets.Facet``: that view
  omits the ``mode`` column and has no ``is_stale`` field (which lives on
  ``compiled_artifacts``), both of which the OKF frontmatter contract
  needs. ``ConceptFacet`` carries exactly the fields the projection reads,
  with no DB-row concerns (``id``, ``agent_id``, ``embed_status``).
- Frontmatter is emitted deterministically: the OKF soft-standard fields
  first in spec order, then every remaining key sorted, so bundles diff
  cleanly in git. Each value is emitted as its JSON form — JSON is a
  subset of YAML, so the output is valid YAML frontmatter and parses back
  to the exact value.
- ``type`` is title-cased for display only; ``tessera_facet_type`` is the
  authoritative round-trip key. Identity round-trips on the ULID in
  ``tessera_external_id``, never on the (mutable) file path.

Mapping contract: ``.docs/okf-integration-enhancement-plan.md``
(OKF <-> Tessera Mapping Contract). Conformance target: OKF v0.1 SPEC.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from tessera.vault.canonical_json import canonical_json
from tessera.vault.skills import SkillsError, slugify

OKF_VERSION: Final[str] = "0.1"

# Tessera extension frontmatter keys. Per OKF SPEC §4.1 consumers must
# tolerate and round-trip unknown keys, so the ``tessera_`` prefix keeps
# these conformant. ``tessera_external_id`` is the durable round-trip
# identity; the slug/path is display-only.
TESSERA_EXTERNAL_ID: Final[str] = "tessera_external_id"
TESSERA_FACET_TYPE: Final[str] = "tessera_facet_type"
TESSERA_MODE: Final[str] = "tessera_mode"
TESSERA_IS_STALE: Final[str] = "tessera_is_stale"
TESSERA_VOLATILITY: Final[str] = "tessera_volatility"
TESSERA_TTL_SECONDS: Final[str] = "tessera_ttl_seconds"
TESSERA_CONTENT_HASH: Final[str] = "tessera_content_hash"

# OKF soft-standard frontmatter fields, emitted in this order ahead of the
# sorted extension/unknown keys (SPEC §4) for deterministic git diffs.
_REQUIRED_ORDER: Final[tuple[str, ...]] = (
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "timestamp",
)

_FENCE: Final[str] = "---"
_CITATIONS_HEADING: Final[str] = "# Citations"
_SLUG_SUFFIX_LEN: Final[int] = 6
_PERSISTENT: Final[str] = "persistent"
_INT_RE: Final[re.Pattern[str]] = re.compile(r"^-?\d+$")


class OKFMappingError(Exception):
    """Base class for OKF mapping failures."""


class OKFParseError(OKFMappingError):
    """A concept's frontmatter block is present but malformed.

    Raised rather than silently treating a broken frontmatter block as
    body text — the strict-input half of SPEC §9: a document with no
    frontmatter is tolerated as body-only, but a frontmatter fence that
    opens and never closes (or a non ``key: value`` line) is an error.
    """


@dataclass(frozen=True, slots=True)
class ConceptFacet:
    """Pure projection input for the facet -> concept mapping.

    A decoupled subset of a vault facet row plus the two fields that live
    outside ``facets.Facet`` — ``mode`` (a ``facets`` column the Facet
    view omits) and ``is_stale`` (a ``compiled_artifacts`` field) — so the
    mapping stays free of the DB-row shape and of any DB/filesystem access.
    """

    external_id: str
    facet_type: str
    content_hash: str
    captured_at: int
    mode: str = "query_time"
    volatility: str = _PERSISTENT
    ttl_seconds: int | None = None
    is_stale: bool | None = None


@dataclass(frozen=True, slots=True)
class ImportedConcept:
    """Inverse of :func:`facet_to_frontmatter`.

    The Tessera fields recovered from a concept's frontmatter. Identity
    resolves on ``tessera_external_id`` (the ULID), never on the path.
    ``facet_type`` prefers the authoritative ``tessera_facet_type`` and
    falls back to un-title-casing the display ``type``. ``extra`` holds
    any frontmatter key not otherwise consumed, so unknown/foreign keys
    survive a round-trip (SPEC §4.1 extension tolerance).
    """

    external_id: str | None
    facet_type: str
    mode: str
    volatility: str | None
    ttl_seconds: int | None
    is_stale: bool | None
    content_hash: str | None
    title: str | None
    description: str | None
    resource: str | None
    tags: tuple[str, ...]
    timestamp: str | None
    display_type: str | None
    extra: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedConcept:
    """A concept document split into frontmatter and body (SPEC §9)."""

    frontmatter: dict[str, Any]
    body: str


def concept_id(facet_type: str, slug: str) -> str:
    """Return the OKF concept ID ``<facet_type>/<slug>``.

    The raw (snake_case) facet type is the top-level bundle directory; the
    slug is the file stem. The ULID is the durable identity (frontmatter),
    not this path.
    """

    return f"{facet_type}/{slug}"


def concept_slug(name: str, external_id: str, taken: Collection[str] = ()) -> str:
    """Return a filesystem-safe slug for a concept's file stem.

    Reuses :func:`skills.slugify`. Falls back to slugifying the ULID when
    ``name`` has no slug-able characters. On collision with an already
    ``taken`` slug, appends a short ULID-derived suffix; the ULID in
    frontmatter remains canonical identity, so the suffix is cosmetic and
    deterministic (no randomness).
    """

    try:
        base = slugify(name)
    except SkillsError:
        base = slugify(external_id)
    if base not in taken:
        return base
    suffix = external_id[-_SLUG_SUFFIX_LEN:].lower()
    return f"{base}-{suffix}"


def facet_to_frontmatter(facet: ConceptFacet, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Project a facet (+ its metadata) onto OKF concept frontmatter.

    Emits the OKF soft-standard fields plus the ``tessera_`` extension
    keys per the mapping contract. ``persistent`` rows omit
    ``tessera_volatility`` to keep noise low; ``tessera_ttl_seconds`` and
    ``tessera_is_stale`` appear only when meaningful. ``type`` is always a
    non-empty display string (conformant per SPEC §9).
    """

    fm: dict[str, Any] = {"type": _display_type(facet.facet_type)}

    title = _nonempty_str(metadata.get("name")) or _nonempty_str(metadata.get("title"))
    if title is not None:
        fm["title"] = title
    description = _nonempty_str(metadata.get("description"))
    if description is not None:
        fm["description"] = description
    resource = _nonempty_str(metadata.get("resource")) or _nonempty_str(metadata.get("disk_path"))
    if resource is not None:
        fm["resource"] = resource
    tags = _str_list(metadata.get("tags"))
    if tags:
        fm["tags"] = list(tags)

    fm["timestamp"] = _format_timestamp(facet.captured_at)

    fm[TESSERA_EXTERNAL_ID] = facet.external_id
    fm[TESSERA_FACET_TYPE] = facet.facet_type
    fm[TESSERA_MODE] = facet.mode
    fm[TESSERA_CONTENT_HASH] = facet.content_hash
    if facet.is_stale is not None:
        fm[TESSERA_IS_STALE] = bool(facet.is_stale)
    if facet.volatility != _PERSISTENT:
        fm[TESSERA_VOLATILITY] = facet.volatility
    if facet.ttl_seconds is not None:
        fm[TESSERA_TTL_SECONDS] = int(facet.ttl_seconds)
    return fm


def frontmatter_to_facet_fields(fm: Mapping[str, Any]) -> ImportedConcept:
    """Recover Tessera fields from a concept's frontmatter (the inverse).

    Identity resolves on ``tessera_external_id``. ``facet_type`` prefers
    ``tessera_facet_type`` (authoritative) and falls back to un-title-casing
    the display ``type``. Unconsumed keys land in ``extra`` for round-trip.
    """

    consumed: set[str] = set()

    def take(key: str) -> Any:
        consumed.add(key)
        return fm.get(key)

    display_type = _as_str_or_none(take("type"))
    raw_facet_type = _as_str_or_none(take(TESSERA_FACET_TYPE))
    facet_type = raw_facet_type or _facet_type_from_display(display_type)

    mode = _as_str_or_none(take(TESSERA_MODE)) or "query_time"

    title = _as_str_or_none(take("title"))
    description = _as_str_or_none(take("description"))
    resource = _as_str_or_none(take("resource"))
    tags = _str_list(take("tags"))
    timestamp = _as_str_or_none(take("timestamp"))
    external_id = _as_str_or_none(take(TESSERA_EXTERNAL_ID))
    content_hash = _as_str_or_none(take(TESSERA_CONTENT_HASH))
    volatility = _as_str_or_none(take(TESSERA_VOLATILITY))
    ttl_seconds = _as_int_or_none(take(TESSERA_TTL_SECONDS))
    is_stale = _as_bool_or_none(take(TESSERA_IS_STALE))

    extra = {key: value for key, value in fm.items() if key not in consumed}

    return ImportedConcept(
        external_id=external_id,
        facet_type=facet_type,
        mode=mode,
        volatility=volatility,
        ttl_seconds=ttl_seconds,
        is_stale=is_stale,
        content_hash=content_hash,
        title=title,
        description=description,
        resource=resource,
        tags=tags,
        timestamp=timestamp,
        display_type=display_type,
        extra=extra,
    )


def render_frontmatter(fm: Mapping[str, Any]) -> str:
    """Serialize a frontmatter mapping into a deterministic YAML block.

    Required OKF fields come first in spec order; every remaining key is
    sorted. Each value is emitted as its JSON form (valid YAML, exact
    round-trip on parse).
    """

    lines = [_FENCE]
    for key in _ordered_keys(fm):
        lines.append(f"{key}: {_emit_scalar(fm[key])}")
    lines.append(_FENCE)
    return "\n".join(lines) + "\n"


def render_concept(
    facet: ConceptFacet,
    metadata: Mapping[str, Any],
    body: str,
    citations: Sequence[str] = (),
) -> str:
    """Render a full concept document: frontmatter block + body (+ citations).

    The ``# Citations`` section (SPEC §8) is appended only when ``citations``
    is non-empty.
    """

    out = render_frontmatter(facet_to_frontmatter(facet, metadata))
    sections: list[str] = []
    body_text = body.strip("\n")
    if body_text:
        sections.append(body_text)
    cite_lines = [line.rstrip("\n") for line in citations if line.strip()]
    if cite_lines:
        sections.append(_CITATIONS_HEADING + "\n\n" + "\n".join(cite_lines))
    if sections:
        out += "\n" + "\n\n".join(sections) + "\n"
    return out


def parse_concept(text: str) -> ParsedConcept:
    """Split a concept document into frontmatter and body (SPEC §9, tolerant).

    A document with no leading frontmatter fence is body-only (tolerated).
    A fence that opens but never closes, or a frontmatter line that is not
    a ``key: value`` pair, raises :class:`OKFParseError` — malformed
    frontmatter never parses silently.
    """

    first_line = text.split("\n", 1)[0]
    if first_line.strip() != _FENCE:
        return ParsedConcept(frontmatter={}, body=text)

    lines = text.split("\n")
    closing_idx: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            closing_idx = index
            break
    if closing_idx is None:
        raise OKFParseError("frontmatter fence opened but never closed")

    frontmatter = _parse_frontmatter(lines[1:closing_idx])
    body = "\n".join(lines[closing_idx + 1 :]).lstrip("\n")
    return ParsedConcept(frontmatter=frontmatter, body=body)


# --------------------------------------------------------------------------
# Internal helpers — pure, no I/O.
# --------------------------------------------------------------------------


def _display_type(facet_type: str) -> str:
    """Title-case a snake_case facet type for display (``compiled_notebook``
    -> ``Compiled Notebook``)."""

    return " ".join(word.capitalize() for word in facet_type.split("_") if word)


def _facet_type_from_display(display_type: str | None) -> str:
    """Inverse of :func:`_display_type` for when ``tessera_facet_type`` is
    absent: ``Compiled Notebook`` -> ``compiled_notebook``."""

    if display_type is None:
        return ""
    return "_".join(display_type.split()).lower()


def _format_timestamp(captured_at: int) -> str:
    """Format an epoch second as an ISO-8601 string, reusing the canonical
    datetime serialization (ADR 0021 ``canonical_json``)."""

    moment = datetime.fromtimestamp(captured_at, tz=UTC)
    return canonical_json(moment).decode("ascii").strip('"')


def _ordered_keys(fm: Mapping[str, Any]) -> list[str]:
    present_required = [key for key in _REQUIRED_ORDER if key in fm]
    rest = sorted(key for key in fm if key not in _REQUIRED_ORDER)
    return present_required + rest


def _emit_scalar(value: Any) -> str:
    # JSON is a subset of YAML, so a JSON-encoded value is valid YAML and
    # parses back to the exact value. ``ensure_ascii=False`` keeps unicode
    # human-readable; ``sort_keys`` keeps nested objects deterministic.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_frontmatter(fm_lines: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    index = 0
    total = len(fm_lines)
    while index < total:
        raw = fm_lines[index]
        if not raw.strip():
            index += 1
            continue
        if raw[:1].isspace():
            raise OKFParseError(
                f"unexpected indentation in frontmatter (nested mappings unsupported): {raw!r}"
            )
        key, sep, value_text = raw.partition(":")
        key = key.strip()
        if not sep or not key:
            raise OKFParseError(f"frontmatter line is not a key: value pair: {raw!r}")
        value_text = value_text.strip()
        if value_text == "":
            items, consumed = _parse_block_list(fm_lines, index + 1)
            if items is not None:
                result[key] = items
                index += 1 + consumed
                continue
            result[key] = None
            index += 1
            continue
        result[key] = _parse_scalar(value_text)
        index += 1
    return result


def _parse_block_list(fm_lines: Sequence[str], start: int) -> tuple[list[Any] | None, int]:
    items: list[Any] = []
    index = start
    while index < len(fm_lines):
        stripped = fm_lines[index].strip()
        if not stripped.startswith("- "):
            break
        items.append(_parse_scalar(stripped[2:].strip()))
        index += 1
    if not items:
        return None, 0
    return items, index - start


def _parse_scalar(text: str) -> Any:
    # Canonical emission is JSON per value, so try JSON first for exact
    # round-trip; fall back to tolerant scalar parsing for hand-authored or
    # foreign frontmatter (SPEC §4.1 / §9 tolerance).
    try:
        return json.loads(text)
    except ValueError:
        pass
    lowered = text.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT_RE.match(text):
        return int(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_flow_items(inner)]
    return _strip_quotes(text)


def _split_flow_items(inner: str) -> list[str]:
    # Split a flow-sequence body on commas not enclosed in quotes, so a
    # quoted element containing a comma survives (e.g. ``a, "b, c"``). Only
    # the foreign / hand-authored fallback path reaches this; Tessera emits
    # lists as valid JSON, handled by ``json.loads`` above.
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote is not None:
            buf.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            buf.append(char)
        elif char == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    items.append("".join(buf).strip())
    return items


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_int_or_none(value: Any) -> int | None:
    # ``bool`` is a subclass of ``int``; exclude it so ``true`` is not an int.
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


__all__ = [
    "OKF_VERSION",
    "TESSERA_CONTENT_HASH",
    "TESSERA_EXTERNAL_ID",
    "TESSERA_FACET_TYPE",
    "TESSERA_IS_STALE",
    "TESSERA_MODE",
    "TESSERA_TTL_SECONDS",
    "TESSERA_VOLATILITY",
    "ConceptFacet",
    "ImportedConcept",
    "OKFMappingError",
    "OKFParseError",
    "ParsedConcept",
    "concept_id",
    "concept_slug",
    "facet_to_frontmatter",
    "frontmatter_to_facet_fields",
    "parse_concept",
    "render_concept",
    "render_frontmatter",
]
