"""Shared importer scaffolding.

The vendor-specific importers (``chatgpt.py``, ``claude.py``) all
share the same boundary contract: they read a JSON export, walk its
shape, and write one facet per conversation through
:mod:`tessera.vault.facets`. The error class hierarchy and the
report dataclass are stable across vendors so the CLI layer can
render both with the same code path; per-vendor parsing logic stays
in each vendor's module.

The constant ``IMPORTABLE_FACET_TYPES`` mirrors the v0.3 spec
constraint that importers backfill v0.1 facet types only —
``person``, ``skill``, ``compiled_notebook`` are reserved for direct
authoring (release-spec.md §v0.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

IMPORTABLE_FACET_TYPES: Final[frozenset[str]] = frozenset(
    {"identity", "preference", "workflow", "project", "style"}
)


class ImportError_(Exception):
    """Base class for importer failures.

    Named with a trailing underscore to avoid shadowing the builtin
    ``ImportError``. Python would let us shadow it, but a wildcard
    import in a caller would surprise tooling that catches the
    builtin.
    """


class MalformedExportError(ImportError_):
    """Export file does not match the expected JSON shape."""


class UnsupportedFacetTypeError(ImportError_):
    """Caller asked for a facet type the importer is not allowed to write."""


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Per-vendor counts after one importer sweep.

    The shape is shared across vendors so the CLI's render path is
    one function rather than one per importer.
    """

    conversations_seen: int = 0
    facets_created: int = 0
    facets_deduplicated: int = 0
    skipped_empty: int = 0
    errors: tuple[str, ...] = ()
    source_path: str = ""


__all__ = [
    "IMPORTABLE_FACET_TYPES",
    "ImportError_",
    "ImportReport",
    "MalformedExportError",
    "UnsupportedFacetTypeError",
]
