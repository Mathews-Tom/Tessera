"""ChatGPT data-export importer.

Reads a ``conversations.json`` file from a ChatGPT data export and
writes one facet per conversation into the vault. The default
``facet_type`` is ``project`` because conversations are work-in-progress
context by default; callers can override to a different v0.1 type
(``identity`` / ``preference`` / ``workflow`` / ``style``) to backfill
a specific facet, but the spec forbids importers from writing
``skill``, ``person``, or ``compiled_notebook`` (release-spec.md §v0.3).

Export shape per OpenAI's documented format:

.. code-block:: json

    [
      {
        "title": "...",
        "create_time": 1700000000.0,
        "mapping": {
          "node-id": {
            "id": "...",
            "message": {
              "author": {"role": "user|assistant|system|tool"},
              "create_time": 1700000050.0,
              "content": {"content_type": "text", "parts": ["..."]}
            },
            "parent": "...",
            "children": ["..."]
          }
        }
      }
    ]

The schema has shifted across export-format versions; we handle
missing fields and shape drift defensively rather than failing
loudly on the first malformed conversation.

Memory: ``json.load`` reads the whole file at once. A 5000-conversation
export is typically 50-150 MB, well within reasonable RAM. Larger
exports (10000+ conversations, multi-GB) would need a streaming
parser; ship that when adoption demands it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlcipher3

from tessera.importers._common import (
    IMPORTABLE_FACET_TYPES,
    ImportError_,
    ImportReport,
    MalformedExportError,
    UnsupportedFacetTypeError,
)
from tessera.vault import facets as vault_facets

_INCLUDED_ROLES: frozenset[str] = frozenset({"user", "assistant"})
_DEFAULT_SOURCE_TOOL: str = "chatgpt-import"


def import_export(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    export_path: Path,
    source_tool: str = _DEFAULT_SOURCE_TOOL,
    facet_type: str = "project",
) -> ImportReport:
    """Import a ChatGPT ``conversations.json`` export.

    Each conversation becomes a single facet whose ``content`` is the
    formatted message log (``# {title}`` header + per-message blocks).
    Messages with empty bodies, system roles, and tool-output roles
    are skipped. Conversations whose entire message log is empty after
    filtering land in ``skipped_empty``; per-conversation parsing
    errors land in ``errors`` and the next conversation is attempted.

    Returns a :class:`ImportReport` with counts. Existing facets are
    detected via :func:`vault.facets.insert`'s content-hash dedup,
    so re-running the importer is a no-op modulo any export updates.
    """

    if facet_type not in IMPORTABLE_FACET_TYPES:
        raise UnsupportedFacetTypeError(
            f"facet_type {facet_type!r} not importable; expected one of "
            f"{sorted(IMPORTABLE_FACET_TYPES)}"
        )
    try:
        raw = export_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MalformedExportError(f"export file not found: {export_path}") from exc
    try:
        conversations = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedExportError(f"export at {export_path} is not valid JSON: {exc}") from exc
    if not isinstance(conversations, list):
        raise MalformedExportError(
            f"expected a JSON array of conversations; got {type(conversations).__name__}"
        )

    seen = 0
    created = 0
    deduped = 0
    skipped_empty = 0
    errors: list[str] = []
    for index, conv in enumerate(conversations):
        seen += 1
        try:
            content = _conversation_to_markdown(conv)
        except MalformedExportError as exc:
            errors.append(f"conversation #{index}: {exc}")
            continue
        if not content.strip():
            skipped_empty += 1
            continue
        captured_at = _conversation_epoch(conv)
        try:
            _, is_new = vault_facets.insert(
                conn,
                agent_id=agent_id,
                facet_type=facet_type,
                content=content,
                source_tool=source_tool,
                metadata={"importer": "chatgpt", "title": _conversation_title(conv)},
                captured_at=captured_at,
            )
        except vault_facets.UnsupportedFacetTypeError as exc:
            errors.append(f"conversation #{index}: {exc}")
            continue
        except vault_facets.UnknownAgentError as exc:
            raise ImportError_(f"agent {agent_id} not found in vault") from exc
        if is_new:
            created += 1
        else:
            deduped += 1
    return ImportReport(
        conversations_seen=seen,
        facets_created=created,
        facets_deduplicated=deduped,
        skipped_empty=skipped_empty,
        errors=tuple(errors),
        source_path=str(export_path),
    )


def _conversation_to_markdown(conv: dict[str, Any]) -> str:
    """Render one conversation as a single Markdown blob.

    Walks the ``mapping`` graph to reconstruct the message timeline.
    The export's ``current_node`` field marks the leaf of the active
    branch; we walk parents from there to the root, then reverse, so
    edited or branched conversations export as the user actually saw
    them rather than the full DAG.
    """

    if not isinstance(conv, dict):
        raise MalformedExportError(f"conversation is not an object: {type(conv).__name__}")
    title = _conversation_title(conv)
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return ""
    timeline = _walk_active_branch(mapping, conv.get("current_node"))
    body_blocks: list[str] = []
    for node in timeline:
        block = _node_to_block(node)
        if block is not None:
            body_blocks.append(block)
    if not body_blocks:
        # Title-only conversations carry no signal worth importing —
        # callers see them in skipped_empty so the report still
        # accounts for every conversation in the export.
        return ""
    return ("# " + title + "\n\n" + "\n\n".join(body_blocks)).rstrip() + "\n"


def _walk_active_branch(mapping: dict[str, Any], current_node: object) -> list[dict[str, Any]]:
    """Walk current_node → root via parent links, then reverse to root → leaf.

    Falls back to a create_time-sorted scan when ``current_node`` is
    missing or its parent chain is broken — older exports sometimes
    omit ``current_node``, and corrupted ones reference dangling
    parents.
    """

    if isinstance(current_node, str) and current_node in mapping:
        timeline_reversed: list[dict[str, Any]] = []
        node_id: str | None = current_node
        seen: set[str] = set()
        while node_id is not None and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id]
            if isinstance(node, dict):
                timeline_reversed.append(node)
                parent = node.get("parent")
                node_id = parent if isinstance(parent, str) else None
            else:
                break
        timeline_reversed.reverse()
        return timeline_reversed
    nodes = [n for n in mapping.values() if isinstance(n, dict)]
    nodes.sort(key=_node_sort_key)
    return nodes


def _node_sort_key(node: dict[str, Any]) -> float:
    msg = node.get("message")
    if isinstance(msg, dict):
        ct = msg.get("create_time")
        if isinstance(ct, int | float):
            return float(ct)
    return float("inf")


def _node_to_block(node: dict[str, Any]) -> str | None:
    """Render one mapping node as a Markdown block, or None to skip."""

    msg = node.get("message")
    if not isinstance(msg, dict):
        return None
    role = _author_role(msg)
    if role not in _INCLUDED_ROLES:
        return None
    text = _message_text(msg)
    if not text.strip():
        return None
    label = "User" if role == "user" else "Assistant"
    return f"## {label}\n\n{text.strip()}"


def _author_role(msg: dict[str, Any]) -> str:
    author = msg.get("author")
    if isinstance(author, dict):
        role = author.get("role")
        if isinstance(role, str):
            return role
    return ""


def _message_text(msg: dict[str, Any]) -> str:
    """Concatenate every text part of a message into a single string.

    The export carries text in ``content.parts`` as either a list of
    strings (older shape) or a list of dicts (newer multimodal shape).
    Image / audio / tool parts are dropped — importers backfill text-
    only context per the v0.3 spec.
    """

    content = msg.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                out.append(text)
    return "\n\n".join(p for p in out if p)


def _conversation_title(conv: dict[str, Any]) -> str:
    title = conv.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return "Untitled conversation"


def _conversation_epoch(conv: dict[str, Any]) -> int | None:
    create = conv.get("create_time")
    if isinstance(create, int | float):
        return int(create)
    return None


__all__ = [
    "ImportError_",
    "ImportReport",
    "MalformedExportError",
    "UnsupportedFacetTypeError",
    "import_export",
]
