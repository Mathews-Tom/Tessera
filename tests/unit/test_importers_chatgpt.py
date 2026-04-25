"""ChatGPT importer + ``tessera import chatgpt`` parser/handler."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.importers import chatgpt as chatgpt_importer
from tessera.migration import bootstrap
from tessera.vault import schema
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        c.execute(stmt)
    c.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'tom', 1)")
    yield c
    c.close()


def _agent_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM agents WHERE external_id = '01A'").fetchone()
    return int(row[0])


def _make_export(tmp_path: Path, conversations: list[Any]) -> Path:
    """Write a synthetic conversations.json under tmp_path.

    ``conversations`` is intentionally typed loosely so tests can pass
    deliberately malformed entries (a bare string in place of a
    conversation object) to exercise the importer's per-conversation
    error path.
    """

    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations), encoding="utf-8")
    return path


def _conversation(
    *,
    title: str,
    messages: list[tuple[str, str, float]],
    create_time: float = 1_700_000_000.0,
) -> dict[str, Any]:
    """Build a minimal mapping-shaped conversation.

    ``messages`` is a list of ``(role, text, create_time)`` tuples in
    chronological order. The synthetic mapping links each node to the
    previous one as a parent so the active-branch walker can recover
    the timeline through the parent chain.
    """

    mapping: dict[str, Any] = {}
    parent: str | None = None
    for i, (role, text, ct) in enumerate(messages):
        node_id = f"node-{i}"
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "children": [f"node-{i + 1}"] if i + 1 < len(messages) else [],
            "message": {
                "id": f"msg-{i}",
                "author": {"role": role},
                "create_time": ct,
                "content": {"content_type": "text", "parts": [text]},
            },
        }
        parent = node_id
    return {
        "title": title,
        "create_time": create_time,
        "mapping": mapping,
        "current_node": f"node-{len(messages) - 1}" if messages else None,
    }


# ---- Module-level importer ----------------------------------------------


@pytest.mark.unit
def test_import_export_creates_one_facet_per_conversation(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                title="rebase strategy",
                messages=[
                    ("user", "How do I squash a branch?", 1_700_000_010.0),
                    ("assistant", "Use git rebase -i HEAD~3.", 1_700_000_020.0),
                ],
            ),
            _conversation(
                title="docker compose",
                messages=[
                    ("user", "Why is my container exiting?", 1_700_000_100.0),
                    ("assistant", "Check the entrypoint.", 1_700_000_110.0),
                ],
            ),
        ],
    )
    report = chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.conversations_seen == 2
    assert report.facets_created == 2
    assert report.facets_deduplicated == 0
    assert report.skipped_empty == 0
    rows = conn.execute(
        "SELECT facet_type, content FROM facets WHERE agent_id = ? ORDER BY id", (aid,)
    ).fetchall()
    assert len(rows) == 2
    assert all(r[0] == "project" for r in rows)
    assert "rebase strategy" in rows[0][1]
    assert "docker compose" in rows[1][1]


@pytest.mark.unit
def test_import_export_dedups_on_rerun(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                title="t",
                messages=[("user", "alpha", 1.0), ("assistant", "beta", 2.0)],
            )
        ],
    )
    first = chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    second = chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    assert first.facets_created == 1
    assert second.facets_created == 0
    assert second.facets_deduplicated == 1


@pytest.mark.unit
def test_import_export_skips_empty_conversations(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [_conversation(title="empty", messages=[("system", "noop", 1.0)])],
    )
    report = chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.skipped_empty == 1
    assert report.facets_created == 0


@pytest.mark.unit
def test_import_export_filters_system_and_tool_roles(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                title="mixed roles",
                messages=[
                    ("system", "You are helpful.", 1.0),
                    ("user", "What's 2+2?", 2.0),
                    ("tool", "calculator: 4", 3.0),
                    ("assistant", "Four.", 4.0),
                ],
            )
        ],
    )
    chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    content = conn.execute("SELECT content FROM facets WHERE agent_id = ?", (aid,)).fetchone()[0]
    assert "You are helpful" not in content
    assert "calculator" not in content
    assert "What's 2+2" in content
    assert "Four." in content


@pytest.mark.unit
def test_import_export_rejects_non_v0_1_facet_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(tmp_path, [])
    with pytest.raises(chatgpt_importer.UnsupportedFacetTypeError):
        chatgpt_importer.import_export(conn, agent_id=aid, export_path=export, facet_type="skill")


@pytest.mark.unit
def test_import_export_rejects_missing_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    with pytest.raises(chatgpt_importer.MalformedExportError, match="not found"):
        chatgpt_importer.import_export(conn, agent_id=aid, export_path=tmp_path / "nope.json")


@pytest.mark.unit
def test_import_export_rejects_non_array_root(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "an array"}), encoding="utf-8")
    with pytest.raises(chatgpt_importer.MalformedExportError, match="JSON array"):
        chatgpt_importer.import_export(conn, agent_id=aid, export_path=bad)


@pytest.mark.unit
def test_import_export_rejects_invalid_json(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    bad = tmp_path / "broken.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(chatgpt_importer.MalformedExportError, match="not valid JSON"):
        chatgpt_importer.import_export(conn, agent_id=aid, export_path=bad)


@pytest.mark.unit
def test_import_export_collects_per_conversation_errors(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A single malformed conversation lands in errors but does not abort
    the rest of the sweep."""

    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            "not an object",  # malformed
            _conversation(title="ok", messages=[("user", "hello", 1.0), ("assistant", "hi", 2.0)]),
        ],
    )
    report = chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.facets_created == 1
    assert len(report.errors) == 1
    assert "conversation #0" in report.errors[0]


@pytest.mark.unit
def test_import_export_handles_multimodal_part_dicts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Newer exports wrap text parts in ``{"text": "..."}`` dicts."""

    aid = _agent_id(conn)
    conv = {
        "title": "multimodal",
        "create_time": 1.0,
        "mapping": {
            "n0": {
                "id": "n0",
                "parent": None,
                "children": [],
                "message": {
                    "author": {"role": "user"},
                    "create_time": 1.0,
                    "content": {
                        "content_type": "text",
                        "parts": [
                            {"text": "hello there"},
                            {"image": "ignored"},
                        ],
                    },
                },
            }
        },
        "current_node": "n0",
    }
    export = _make_export(tmp_path, [conv])
    chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    content = conn.execute("SELECT content FROM facets WHERE agent_id = ?", (aid,)).fetchone()[0]
    assert "hello there" in content
    assert "ignored" not in content


@pytest.mark.unit
def test_import_export_falls_back_to_create_time_walk_when_current_node_missing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An export with no ``current_node`` still imports via timestamp sort."""

    aid = _agent_id(conn)
    conv = _conversation(
        title="no current_node",
        messages=[("user", "first", 1.0), ("assistant", "second", 2.0)],
    )
    conv["current_node"] = None
    export = _make_export(tmp_path, [conv])
    report = chatgpt_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.facets_created == 1


# ---- CLI handler --------------------------------------------------------


@pytest.fixture
def initialized_vault(tmp_path: Path, passphrase: bytearray) -> Path:
    vault_path = tmp_path / "vault.db"
    salt = new_salt()
    save_salt(vault_path, salt)
    key = derive_key(passphrase, salt)
    bootstrap(vault_path, key)
    key.wipe()
    return vault_path


def _seed_agent_in_vault(vault_path: Path, passphrase: bytearray) -> None:
    salt_bytes = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    key = derive_key(passphrase, salt_bytes)
    with VaultConnection.open(vault_path, key) as vc:
        vc.connection.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'tom', 1)"
        )
    key.wipe()


@pytest.mark.unit
def test_cli_import_chatgpt_runs_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
    tmp_path: Path,
) -> None:
    _seed_agent_in_vault(initialized_vault, passphrase)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                title="t",
                messages=[("user", "hello", 1.0), ("assistant", "hi", 2.0)],
            )
        ],
    )
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "import",
            "chatgpt",
            str(export),
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 0


@pytest.mark.unit
def test_cli_import_chatgpt_surfaces_malformed_export(
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_agent_in_vault(initialized_vault, passphrase)
    bad = tmp_path / "broken.json"
    bad.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "import",
            "chatgpt",
            str(bad),
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err


@pytest.mark.unit
def test_cli_import_subparser_requires_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["import"])
    err = capsys.readouterr().err
    assert "import_command" in err or "required" in err
