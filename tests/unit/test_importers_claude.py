"""Claude importer + ``tessera import claude`` parser/handler."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.importers import claude as claude_importer
from tessera.importers._common import MalformedExportError, UnsupportedFacetTypeError
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
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations), encoding="utf-8")
    return path


def _conversation(
    *,
    name: str,
    messages: list[tuple[str, str, str]],
    created_at: str = "2024-01-01T12:00:00.000Z",
    use_content_blocks: bool = False,
) -> dict[str, Any]:
    """Build a Claude-shape conversation.

    ``messages`` is ``(sender, text, created_at)`` per message.
    ``use_content_blocks`` toggles between the older ``text`` field
    and the newer ``content`` block array shape so tests cover both
    schemas the importer is documented to handle.
    """

    chat: list[dict[str, Any]] = []
    for sender, text, ts in messages:
        msg: dict[str, Any] = {
            "uuid": f"msg-{len(chat)}",
            "sender": sender,
            "created_at": ts,
        }
        if use_content_blocks:
            msg["content"] = [{"type": "text", "text": text}]
        else:
            msg["text"] = text
        chat.append(msg)
    return {
        "uuid": "conv-uuid",
        "name": name,
        "created_at": created_at,
        "updated_at": created_at,
        "chat_messages": chat,
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
                name="rebase strategy",
                messages=[
                    ("human", "How do I squash a branch?", "2024-01-01T12:00:00.000Z"),
                    ("assistant", "Use git rebase -i.", "2024-01-01T12:00:30.000Z"),
                ],
            ),
            _conversation(
                name="docker compose",
                messages=[
                    ("human", "Why is my container exiting?", "2024-01-02T12:00:00.000Z"),
                    ("assistant", "Check the entrypoint.", "2024-01-02T12:00:30.000Z"),
                ],
            ),
        ],
    )
    report = claude_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.conversations_seen == 2
    assert report.facets_created == 2
    assert report.facets_deduplicated == 0
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
                name="t",
                messages=[
                    ("human", "alpha", "2024-01-01T12:00:00.000Z"),
                    ("assistant", "beta", "2024-01-01T12:00:30.000Z"),
                ],
            )
        ],
    )
    first = claude_importer.import_export(conn, agent_id=aid, export_path=export)
    second = claude_importer.import_export(conn, agent_id=aid, export_path=export)
    assert first.facets_created == 1
    assert second.facets_created == 0
    assert second.facets_deduplicated == 1


@pytest.mark.unit
def test_import_export_handles_content_block_schema(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Newer exports use ``content: [{"type": "text", "text": "..."}]``."""

    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                name="multimodal",
                messages=[
                    ("human", "hello there", "2024-01-01T12:00:00.000Z"),
                    ("assistant", "hi from blocks", "2024-01-01T12:00:30.000Z"),
                ],
                use_content_blocks=True,
            )
        ],
    )
    claude_importer.import_export(conn, agent_id=aid, export_path=export)
    content = conn.execute("SELECT content FROM facets WHERE agent_id = ?", (aid,)).fetchone()[0]
    assert "hello there" in content
    assert "hi from blocks" in content


@pytest.mark.unit
def test_import_export_drops_non_text_content_blocks(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Image / tool blocks land in ``content`` alongside text blocks."""

    aid = _agent_id(conn)
    conv = {
        "uuid": "u",
        "name": "mixed",
        "created_at": "2024-01-01T12:00:00.000Z",
        "chat_messages": [
            {
                "sender": "human",
                "created_at": "2024-01-01T12:00:00.000Z",
                "content": [
                    {"type": "text", "text": "describe this image"},
                    {"type": "image", "source": {"data": "<binary>"}},
                ],
            },
            {
                "sender": "assistant",
                "created_at": "2024-01-01T12:00:30.000Z",
                "content": [{"type": "text", "text": "looks like a cat"}],
            },
        ],
    }
    export = _make_export(tmp_path, [conv])
    claude_importer.import_export(conn, agent_id=aid, export_path=export)
    content = conn.execute("SELECT content FROM facets WHERE agent_id = ?", (aid,)).fetchone()[0]
    assert "describe this image" in content
    assert "binary" not in content
    assert "looks like a cat" in content


@pytest.mark.unit
def test_import_export_skips_empty_conversations(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [_conversation(name="empty", messages=[("system", "noop", "2024-01-01T12:00:00.000Z")])],
    )
    report = claude_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.skipped_empty == 1
    assert report.facets_created == 0


@pytest.mark.unit
def test_import_export_filters_non_human_assistant_senders(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                name="mixed roles",
                messages=[
                    ("system", "noise", "2024-01-01T12:00:00.000Z"),
                    ("human", "real question", "2024-01-01T12:00:30.000Z"),
                    ("tool", "tool output", "2024-01-01T12:01:00.000Z"),
                    ("assistant", "real answer", "2024-01-01T12:01:30.000Z"),
                ],
            )
        ],
    )
    claude_importer.import_export(conn, agent_id=aid, export_path=export)
    content = conn.execute("SELECT content FROM facets WHERE agent_id = ?", (aid,)).fetchone()[0]
    assert "noise" not in content
    assert "tool output" not in content
    assert "real question" in content
    assert "real answer" in content


@pytest.mark.unit
def test_import_export_parses_iso_timestamp_into_captured_at(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            _conversation(
                name="t",
                created_at="2024-06-15T10:30:00.000Z",
                messages=[
                    ("human", "x", "2024-06-15T10:30:00.000Z"),
                    ("assistant", "y", "2024-06-15T10:30:30.000Z"),
                ],
            )
        ],
    )
    claude_importer.import_export(conn, agent_id=aid, export_path=export)
    captured_at = conn.execute(
        "SELECT captured_at FROM facets WHERE agent_id = ?", (aid,)
    ).fetchone()[0]
    # 2024-06-15T10:30:00Z = 1718447400 epoch seconds
    assert int(captured_at) == 1718447400


@pytest.mark.unit
def test_import_export_handles_unparseable_timestamp(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Bad ``created_at`` falls back to the captured_at default."""

    aid = _agent_id(conn)
    conv = _conversation(
        name="t",
        messages=[
            ("human", "x", "garbage"),
            ("assistant", "y", "garbage"),
        ],
    )
    conv["created_at"] = "not a timestamp"
    export = _make_export(tmp_path, [conv])
    report = claude_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.facets_created == 1


@pytest.mark.unit
def test_import_export_rejects_non_v0_1_facet_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(tmp_path, [])
    with pytest.raises(UnsupportedFacetTypeError):
        claude_importer.import_export(conn, agent_id=aid, export_path=export, facet_type="skill")


@pytest.mark.unit
def test_import_export_rejects_non_array_root(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "array"}), encoding="utf-8")
    with pytest.raises(MalformedExportError, match="JSON array"):
        claude_importer.import_export(conn, agent_id=aid, export_path=bad)


@pytest.mark.unit
def test_import_export_rejects_invalid_json(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    bad = tmp_path / "broken.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(MalformedExportError, match="not valid JSON"):
        claude_importer.import_export(conn, agent_id=aid, export_path=bad)


@pytest.mark.unit
def test_import_export_collects_per_conversation_errors(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    export = _make_export(
        tmp_path,
        [
            "not an object",
            _conversation(
                name="ok",
                messages=[
                    ("human", "alpha", "2024-01-01T12:00:00.000Z"),
                    ("assistant", "beta", "2024-01-01T12:00:30.000Z"),
                ],
            ),
        ],
    )
    report = claude_importer.import_export(conn, agent_id=aid, export_path=export)
    assert report.facets_created == 1
    assert len(report.errors) == 1
    assert "conversation #0" in report.errors[0]


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
def test_cli_import_claude_runs_end_to_end(
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
                name="t",
                messages=[
                    ("human", "hello", "2024-01-01T12:00:00.000Z"),
                    ("assistant", "hi", "2024-01-01T12:00:30.000Z"),
                ],
            )
        ],
    )
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "import",
            "claude",
            str(export),
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 0


@pytest.mark.unit
def test_cli_import_claude_surfaces_malformed_export(
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
            "claude",
            str(bad),
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err
