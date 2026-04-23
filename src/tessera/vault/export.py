"""Vault export: JSON (canonical), Markdown (per-facet-type), SQLite (decrypted plain copy).

Per ``docs/release-spec.md §v0.1 DoD``:
- JSON is the canonical round-trippable format. ``export → import → export``
  produces byte-equivalent JSON given the same vault state.
- Markdown is human-readable, one file per facet type. Not round-trippable.
- SQLite is a plain-text decrypted copy of the vault. Not round-trippable
  via this module; reading it back requires SQLite tooling directly.

Embed columns (``embed_model_id``, ``embed_status``, ``embed_attempts``,
``embed_last_error``, ``embed_last_attempt_at``) are deliberately omitted
from every format. They are machine-local artifacts tied to a specific
embedding model revision; preserving them across an export/import cycle
would pretend the re-imported vault is still consistent with the
original model slot when it is not. Re-import triggers re-embed.

Soft-deleted facets (``is_deleted = 1``) are included only when
``include_deleted`` is true. When included, the ``is_deleted`` and
``deleted_at`` fields are preserved; when excluded, they are filtered
out before serialisation so the exported artifact looks like a clean
vault at the moment of soft-delete.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import sqlcipher3

from tessera.vault.connection import VaultConnection

EXPORT_SCHEMA_VERSION: Final[int] = 1

_FACET_TYPES_V01: Final[tuple[str, ...]] = (
    "identity",
    "preference",
    "workflow",
    "project",
    "style",
)


@dataclass(frozen=True, slots=True)
class ExportSummary:
    """How many rows of each kind landed in the output artifact."""

    agents: int
    facets: int
    facets_by_type: dict[str, int]
    output_path: Path
    format: str


def export_json(
    vault: VaultConnection,
    *,
    output_path: Path,
    include_deleted: bool = False,
    now_epoch: int = 0,
) -> ExportSummary:
    """Write the canonical JSON export to ``output_path``.

    The JSON document is stable byte-for-byte across exports of the
    same vault state: keys are sorted, facet rows are ordered by
    ``external_id``, and field ordering inside each row is lexical.
    The ``exported_at`` field uses ``now_epoch`` so tests can pin it;
    callers from production pass the real clock.
    """

    document = _build_document(vault, include_deleted=include_deleted, now_epoch=now_epoch)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return _summary(document, output_path, "json")


def export_markdown(
    vault: VaultConnection,
    *,
    output_dir: Path,
    include_deleted: bool = False,
) -> ExportSummary:
    """Write one Markdown file per facet type under ``output_dir``.

    File naming is ``<facet_type>.md``. Each file contains a YAML-style
    header with export metadata and one section per facet, ordered by
    ``captured_at`` descending. The format is human-readable; re-import
    is not a supported round-trip.
    """

    document = _build_document(vault, include_deleted=include_deleted, now_epoch=0)
    output_dir.mkdir(parents=True, exist_ok=True)
    by_type: dict[str, list[dict[str, Any]]] = {t: [] for t in _FACET_TYPES_V01}
    for facet in document["facets"]:
        by_type.setdefault(facet["facet_type"], []).append(facet)

    for facet_type, rows in by_type.items():
        rows.sort(key=lambda r: r["captured_at"], reverse=True)
        path = output_dir / f"{facet_type}.md"
        path.write_text(_render_markdown(facet_type, rows, document["vault_id"]))

    return _summary(document, output_dir, "md")


def export_sqlite(
    vault: VaultConnection,
    *,
    output_path: Path,
    include_deleted: bool = False,
) -> ExportSummary:
    """Write a plain (non-encrypted) SQLite copy to ``output_path``.

    The source vault is sqlcipher-encrypted; the destination is a plain
    sqlite3 file so the user can open it with any SQLite tool. Row-level
    content matches the source at the moment of export for the
    ``agents`` and ``facets`` tables; the audit log, embedding-model
    registry, and capability tables are deliberately not exported —
    they contain operational state that an importer would not
    reconstruct semantically.

    Embed columns are omitted, matching the JSON and Markdown exports.
    Soft-deleted rows respect ``include_deleted``.
    """

    if output_path.exists():
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    agent_rows = _fetch_agents(vault.connection)
    facet_rows = _fetch_facets(vault.connection, include_deleted=include_deleted)

    dst = sqlite3.connect(output_path)
    try:
        dst.executescript(_PLAIN_SCHEMA)
        dst.executemany(
            "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
            [(a["external_id"], a["name"], a["created_at"]) for a in agent_rows],
        )
        external_to_id = {
            row[0]: row[1] for row in dst.execute("SELECT external_id, id FROM agents").fetchall()
        }
        dst.executemany(
            """
            INSERT INTO facets(
                external_id, agent_id, facet_type, content, content_hash,
                mode, source_tool, captured_at, metadata, is_deleted, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f["external_id"],
                    external_to_id[f["agent_external_id"]],
                    f["facet_type"],
                    f["content"],
                    f["content_hash"],
                    f["mode"],
                    f["source_tool"],
                    f["captured_at"],
                    json.dumps(f["metadata"], sort_keys=True),
                    f["is_deleted"],
                    f["deleted_at"],
                )
                for f in facet_rows
            ],
        )
        dst.commit()
    finally:
        dst.close()

    document = {
        "agents": agent_rows,
        "facets": facet_rows,
        "vault_id": vault.state.vault_id,
    }
    return _summary(document, output_path, "sqlite")


def _build_document(
    vault: VaultConnection, *, include_deleted: bool, now_epoch: int
) -> dict[str, Any]:
    agents = _fetch_agents(vault.connection)
    facets = _fetch_facets(vault.connection, include_deleted=include_deleted)
    return {
        "tessera_export_version": EXPORT_SCHEMA_VERSION,
        "vault_id": vault.state.vault_id,
        "schema_version": vault.state.schema_version,
        "exported_at": now_epoch,
        "include_deleted": include_deleted,
        "agents": agents,
        "facets": facets,
    }


def _fetch_agents(conn: sqlcipher3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT external_id, name, created_at FROM agents ORDER BY external_id"
    ).fetchall()
    return [{"external_id": r[0], "name": r[1], "created_at": r[2]} for r in rows]


def _fetch_facets(conn: sqlcipher3.Connection, *, include_deleted: bool) -> list[dict[str, Any]]:
    where = "" if include_deleted else " WHERE f.is_deleted = 0"
    rows = conn.execute(
        f"""
        SELECT f.external_id, a.external_id, f.facet_type, f.content,
               f.content_hash, f.mode, f.source_tool, f.captured_at,
               f.metadata, f.is_deleted, f.deleted_at
        FROM facets f
        JOIN agents a ON a.id = f.agent_id
        {where}
        ORDER BY f.external_id
        """
    ).fetchall()
    return [
        {
            "external_id": r[0],
            "agent_external_id": r[1],
            "facet_type": r[2],
            "content": r[3],
            "content_hash": r[4],
            "mode": r[5],
            "source_tool": r[6],
            "captured_at": r[7],
            "metadata": json.loads(r[8]) if r[8] else {},
            "is_deleted": r[9],
            "deleted_at": r[10],
        }
        for r in rows
    ]


def _summary(document: dict[str, Any], output_path: Path, format_name: str) -> ExportSummary:
    by_type: dict[str, int] = {}
    for facet in document["facets"]:
        by_type[facet["facet_type"]] = by_type.get(facet["facet_type"], 0) + 1
    return ExportSummary(
        agents=len(document["agents"]),
        facets=len(document["facets"]),
        facets_by_type=by_type,
        output_path=output_path,
        format=format_name,
    )


def _render_markdown(facet_type: str, rows: list[dict[str, Any]], vault_id: str) -> str:
    lines: list[str] = [
        f"# {facet_type.capitalize()} facets",
        "",
        f"Vault: `{vault_id}`",
        f"Exported: {len(rows)} facet{'s' if len(rows) != 1 else ''}",
        "",
        "---",
        "",
    ]
    for row in rows:
        soft_del = " (soft-deleted)" if row["is_deleted"] else ""
        lines.extend(
            [
                f"## `{row['external_id']}`{soft_del}",
                "",
                f"- Agent: `{row['agent_external_id']}`",
                f"- Captured at: {row['captured_at']}",
                f"- Source tool: `{row['source_tool']}`",
                f"- Mode: `{row['mode']}`",
                f"- Content hash: `{row['content_hash']}`",
                "",
                "### Content",
                "",
                row["content"],
                "",
            ]
        )
        if row["metadata"]:
            lines.extend(
                [
                    "### Metadata",
                    "",
                    "```json",
                    json.dumps(row["metadata"], indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_PLAIN_SCHEMA: Final[str] = """
CREATE TABLE agents (
    id          INTEGER PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE facets (
    id            INTEGER PRIMARY KEY,
    external_id   TEXT NOT NULL UNIQUE,
    agent_id      INTEGER NOT NULL REFERENCES agents(id),
    facet_type    TEXT NOT NULL,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    mode          TEXT NOT NULL,
    source_tool   TEXT NOT NULL,
    captured_at   INTEGER NOT NULL,
    metadata      TEXT NOT NULL,
    is_deleted    INTEGER NOT NULL,
    deleted_at    INTEGER
);
CREATE INDEX facets_agent_type ON facets(agent_id, facet_type, captured_at DESC);
"""


def import_json(
    vault: VaultConnection, *, document_path: Path, agent_external_id: str | None = None
) -> ExportSummary:
    """Import a JSON export into an open vault.

    ``document_path`` points at a JSON file previously written by
    :func:`export_json`. The importer is strict: ``tessera_export_version``
    must match, and every facet row's ``agent_external_id`` must resolve
    to an existing agent in the target vault. When ``agent_external_id``
    is supplied, the importer rewrites every facet's agent pointer to
    that value — useful for re-importing an export into a vault whose
    agent IDs were recreated.

    The function does not validate content-hash uniqueness: callers
    that re-import into a vault that already has the exported rows
    will hit the ``UNIQUE(agent_id, content_hash)`` constraint and see
    a loud failure, which is the documented expectation.

    ``embed_*`` columns are left at their schema defaults, so the
    vault's embed worker will pick up the newly imported facets and
    embed them against whatever model is currently active.
    """

    document = json.loads(document_path.read_text())
    if document.get("tessera_export_version") != EXPORT_SCHEMA_VERSION:
        raise ValueError(
            f"export schema version mismatch: got "
            f"{document.get('tessera_export_version')!r}, expected {EXPORT_SCHEMA_VERSION}"
        )

    agent_rows = document["agents"]
    facet_rows = document["facets"]
    conn = vault.connection
    agent_id_by_external: dict[str, int] = {}

    if agent_external_id is None:
        # Import agents that don't already exist; return existing IDs for ones that do.
        for agent in agent_rows:
            existing = conn.execute(
                "SELECT id FROM agents WHERE external_id = ?", (agent["external_id"],)
            ).fetchone()
            if existing is not None:
                agent_id_by_external[agent["external_id"]] = int(existing[0])
                continue
            cur = conn.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
                (agent["external_id"], agent["name"], agent["created_at"]),
            )
            agent_id_by_external[agent["external_id"]] = int(cur.lastrowid or 0)
    else:
        resolved = conn.execute(
            "SELECT id FROM agents WHERE external_id = ?", (agent_external_id,)
        ).fetchone()
        if resolved is None:
            raise ValueError(f"--agent-external-id {agent_external_id!r} not found in target vault")
        target_id = int(resolved[0])
        for agent in agent_rows:
            agent_id_by_external[agent["external_id"]] = target_id

    for facet in facet_rows:
        agent_id = agent_id_by_external[facet["agent_external_id"]]
        conn.execute(
            """
            INSERT INTO facets(
                external_id, agent_id, facet_type, content, content_hash,
                mode, source_tool, captured_at, metadata, is_deleted, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                facet["external_id"],
                agent_id,
                facet["facet_type"],
                facet["content"],
                facet["content_hash"],
                facet["mode"],
                facet["source_tool"],
                facet["captured_at"],
                json.dumps(facet["metadata"], sort_keys=True),
                facet["is_deleted"],
                facet["deleted_at"],
            ),
        )
    conn.commit()
    return _summary(document, document_path, "json")


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "ExportSummary",
    "export_json",
    "export_markdown",
    "export_sqlite",
    "import_json",
]
