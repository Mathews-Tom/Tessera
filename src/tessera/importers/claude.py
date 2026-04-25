"""Claude data-export importer.

Reads a ``conversations.json`` file from a Claude data export and
writes one facet per conversation. Default ``facet_type`` is
``project`` for the same reason as the ChatGPT importer:
conversations are work-in-progress context. The v0.1-only
constraint (no ``skill`` / ``person`` / ``compiled_notebook``) is
shared via :mod:`tessera.importers._common`.

Export shape per Anthropic's data-export format:

.. code-block:: json

    [
      {
        "uuid": "...",
        "name": "...",
        "created_at": "2024-01-01T12:00:00.000Z",
        "updated_at": "2024-01-01T12:30:00.000Z",
        "chat_messages": [
          {
            "uuid": "...",
            "text": "the user message",
            "sender": "human",
            "created_at": "2024-01-01T12:00:00.000Z",
            "content": [{"type": "text", "text": "..."}]
          },
          {
            "text": "the assistant response",
            "sender": "assistant",
            "created_at": "2024-01-01T12:00:30.000Z"
          }
        ]
      }
    ]

The shape is simpler than ChatGPT's mapping graph — Claude exports
use a flat ``chat_messages`` array per conversation with no
branching. Timestamps are ISO 8601 strings; the importer converts
them to Unix epoch for the facet's ``captured_at``.

Newer exports use the multi-block ``content`` array on each message
(``[{"type": "text", "text": "..."}, {"type": "image", ...}]``);
older exports carry text directly in the ``text`` field. We prefer
``content`` when present and fall back to ``text``, so both formats
import cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime
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

_INCLUDED_SENDERS: frozenset[str] = frozenset({"human", "assistant"})
_DEFAULT_SOURCE_TOOL: str = "claude-import"


def import_export(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    export_path: Path,
    source_tool: str = _DEFAULT_SOURCE_TOOL,
    facet_type: str = "project",
) -> ImportReport:
    """Import a Claude ``conversations.json`` export.

    Each conversation becomes a single facet. Messages with empty
    bodies and senders outside ``human`` / ``assistant`` are
    skipped. Conversations whose entire message log is empty after
    filtering land in ``skipped_empty``; per-conversation parsing
    errors land in ``errors`` and the next conversation is
    attempted. Re-running is idempotent via the existing
    ``UNIQUE(agent_id, content_hash)`` dedup path.
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
                metadata={"importer": "claude", "title": _conversation_title(conv)},
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
    """Render one conversation as a single Markdown blob."""

    if not isinstance(conv, dict):
        raise MalformedExportError(f"conversation is not an object: {type(conv).__name__}")
    title = _conversation_title(conv)
    chat_messages = conv.get("chat_messages")
    if not isinstance(chat_messages, list):
        return ""
    body_blocks: list[str] = []
    for msg in chat_messages:
        if not isinstance(msg, dict):
            continue
        block = _message_to_block(msg)
        if block is not None:
            body_blocks.append(block)
    if not body_blocks:
        return ""
    return ("# " + title + "\n\n" + "\n\n".join(body_blocks)).rstrip() + "\n"


def _message_to_block(msg: dict[str, Any]) -> str | None:
    sender = msg.get("sender")
    if not isinstance(sender, str) or sender not in _INCLUDED_SENDERS:
        return None
    text = _message_text(msg)
    if not text.strip():
        return None
    label = "User" if sender == "human" else "Assistant"
    return f"## {label}\n\n{text.strip()}"


def _message_text(msg: dict[str, Any]) -> str:
    """Concatenate text parts, preferring ``content`` over ``text``.

    Newer exports carry text in ``content`` as a list of typed blocks
    (``[{"type": "text", "text": "..."}]``) alongside multimodal
    blocks (image, tool, etc.). Older exports carry plain text in
    the top-level ``text`` field. We try ``content`` first and fall
    back when it is absent or empty so both formats import cleanly.
    """

    content = msg.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in (None, "text"):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        if parts:
            return "\n\n".join(parts)
    text = msg.get("text")
    if isinstance(text, str):
        return text
    return ""


def _conversation_title(conv: dict[str, Any]) -> str:
    name = conv.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "Untitled conversation"


def _conversation_epoch(conv: dict[str, Any]) -> int | None:
    """Convert Claude's ISO 8601 ``created_at`` into a Unix epoch.

    Returns None on missing or unparseable timestamps. The ``Z``
    suffix maps to UTC explicitly; ``fromisoformat`` from Python
    3.11 onward accepts it directly.
    """

    created = conv.get("created_at")
    if not isinstance(created, str):
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp())


__all__ = ["import_export"]
