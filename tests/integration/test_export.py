"""Vault export and round-trip — JSON, Markdown, SQLite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tessera.migration import bootstrap
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt
from tessera.vault.export import (
    EXPORT_SCHEMA_VERSION,
    export_json,
    export_markdown,
    export_sqlite,
    import_json,
)


def _seed_vault(vault_path: Path, passphrase: bytes, *, with_deleted: bool = True) -> None:
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(passphrase), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            conn = vc.connection
            conn.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES ('01AGT', 'daisy', 1_000_000)"
            )
            agent_id = int(
                conn.execute("SELECT id FROM agents WHERE external_id='01AGT'").fetchone()[0]
            )
            for i, ft in enumerate(("identity", "preference", "workflow", "project", "style")):
                vault_capture.capture(
                    conn,
                    agent_id=agent_id,
                    facet_type=ft,
                    content=f"{ft} example {i}",
                    source_tool="test",
                    captured_at=1_000_000 + i,
                )
            if with_deleted:
                vault_capture.capture(
                    conn,
                    agent_id=agent_id,
                    facet_type="project",
                    content="soft-deleted note",
                    source_tool="test",
                    captured_at=1_000_999,
                )
                conn.execute(
                    "UPDATE facets SET is_deleted = 1, deleted_at = 1_001_000 "
                    "WHERE content = 'soft-deleted note'"
                )


@pytest.fixture
def seeded_vault(tmp_path: Path) -> tuple[Path, bytes]:
    vault_path = tmp_path / "vault.db"
    passphrase = b"export-roundtrip"
    _seed_vault(vault_path, passphrase)
    return vault_path, passphrase


@pytest.mark.integration
def test_json_export_shape_and_determinism(
    seeded_vault: tuple[Path, bytes], tmp_path: Path
) -> None:
    vault_path, passphrase = seeded_vault
    out1 = tmp_path / "export1.json"
    out2 = tmp_path / "export2.json"

    salt = _reload_salt(vault_path)
    with derive_key(bytearray(passphrase), salt) as key:
        with VaultConnection.open(vault_path, key) as vc:
            export_json(vc, output_path=out1, now_epoch=1234)
        with VaultConnection.open(vault_path, key) as vc:
            export_json(vc, output_path=out2, now_epoch=1234)

    # Byte-equivalent across two exports of the same state with pinned clock.
    assert out1.read_bytes() == out2.read_bytes()

    doc = json.loads(out1.read_text())
    assert doc["tessera_export_version"] == EXPORT_SCHEMA_VERSION
    assert doc["include_deleted"] is False
    assert len(doc["agents"]) == 1
    # 5 live facets, soft-deleted excluded by default.
    assert len(doc["facets"]) == 5
    assert {f["facet_type"] for f in doc["facets"]} == {
        "identity",
        "preference",
        "workflow",
        "project",
        "style",
    }
    # Embed columns never cross the boundary.
    for facet in doc["facets"]:
        for banned in ("embed_model_id", "embed_status", "embed_attempts"):
            assert banned not in facet


@pytest.mark.integration
def test_json_export_include_deleted(seeded_vault: tuple[Path, bytes], tmp_path: Path) -> None:
    vault_path, passphrase = seeded_vault
    salt = _reload_salt(vault_path)
    out = tmp_path / "with-deleted.json"
    with (
        derive_key(bytearray(passphrase), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        export_json(vc, output_path=out, include_deleted=True, now_epoch=0)

    doc = json.loads(out.read_text())
    assert doc["include_deleted"] is True
    deleted = [f for f in doc["facets"] if f["is_deleted"]]
    assert len(deleted) == 1
    assert deleted[0]["content"] == "soft-deleted note"
    assert deleted[0]["deleted_at"] == 1_001_000


@pytest.mark.integration
def test_json_round_trip_is_byte_equivalent(
    seeded_vault: tuple[Path, bytes], tmp_path: Path
) -> None:
    # Export → wipe agents+facets → re-import → re-export.
    # Keeping the same vault means the outer envelope (vault_id,
    # schema_version) is identical across both exports, so a full
    # byte-equal comparison validates that the round-trip preserves
    # every agent+facet row plus every field within them.
    vault_path, passphrase = seeded_vault
    export_1 = tmp_path / "export1.json"
    export_2 = tmp_path / "export2.json"
    salt = _reload_salt(vault_path)
    with derive_key(bytearray(passphrase), salt) as key:
        with VaultConnection.open(vault_path, key) as vc:
            export_json(vc, output_path=export_1, include_deleted=True, now_epoch=42)
        with VaultConnection.open(vault_path, key) as vc:
            # Wipe the content; leave the schema, embedding_models, etc. alone.
            # FKs from audit_log / capabilities reference agents, so turn them
            # off for the test-only wipe. Production import/export never does
            # this — callers bring up a fresh vault or import alongside the
            # existing rows and hit the UNIQUE constraint loudly.
            vc.connection.execute("PRAGMA foreign_keys = OFF")
            vc.connection.execute("DELETE FROM facets")
            vc.connection.execute("DELETE FROM agents")
            vc.connection.execute("PRAGMA foreign_keys = ON")
            vc.connection.commit()
        with VaultConnection.open(vault_path, key) as vc:
            import_json(vc, document_path=export_1)
        with VaultConnection.open(vault_path, key) as vc:
            export_json(vc, output_path=export_2, include_deleted=True, now_epoch=42)

    assert export_1.read_bytes() == export_2.read_bytes()


@pytest.mark.integration
def test_markdown_export_is_per_facet_type(
    seeded_vault: tuple[Path, bytes], tmp_path: Path
) -> None:
    vault_path, passphrase = seeded_vault
    salt = _reload_salt(vault_path)
    out_dir = tmp_path / "md"
    with (
        derive_key(bytearray(passphrase), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        summary = export_markdown(vc, output_dir=out_dir)

    # Five .md files, one per v0.1 facet type.
    produced = {p.name for p in out_dir.iterdir()}
    assert produced == {
        "identity.md",
        "preference.md",
        "workflow.md",
        "project.md",
        "style.md",
    }
    assert summary.facets == 5
    assert summary.format == "md"

    # Every live facet content appears in exactly one per-type file.
    identity_text = (out_dir / "identity.md").read_text()
    assert "identity example 0" in identity_text
    assert "preference example" not in identity_text


@pytest.mark.integration
def test_sqlite_export_is_plain_and_queryable(
    seeded_vault: tuple[Path, bytes], tmp_path: Path
) -> None:
    vault_path, passphrase = seeded_vault
    salt = _reload_salt(vault_path)
    out_path = tmp_path / "plain.db"
    with (
        derive_key(bytearray(passphrase), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        summary = export_sqlite(vc, output_path=out_path, include_deleted=True)

    assert summary.facets == 6  # five live + one soft-deleted

    # Plain sqlite3 (not sqlcipher) can open it.
    plain = sqlite3.connect(out_path)
    try:
        agent_rows = plain.execute("SELECT external_id, name FROM agents").fetchall()
        assert agent_rows == [("01AGT", "daisy")]
        facet_count = plain.execute("SELECT COUNT(*) FROM facets").fetchone()[0]
        assert facet_count == 6
        types = {
            row[0] for row in plain.execute("SELECT DISTINCT facet_type FROM facets").fetchall()
        }
        assert types == {"identity", "preference", "workflow", "project", "style"}
    finally:
        plain.close()


@pytest.mark.integration
def test_import_version_mismatch_is_loud(seeded_vault: tuple[Path, bytes], tmp_path: Path) -> None:
    vault_path, passphrase = seeded_vault
    salt = _reload_salt(vault_path)
    bogus = tmp_path / "bogus.json"
    bogus.write_text(json.dumps({"tessera_export_version": 999, "agents": [], "facets": []}))
    with (
        derive_key(bytearray(passphrase), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
        pytest.raises(ValueError, match="schema version"),
    ):
        import_json(vc, document_path=bogus)


@pytest.mark.integration
def test_import_agent_remap(seeded_vault: tuple[Path, bytes], tmp_path: Path) -> None:
    src_path, passphrase = seeded_vault
    export_path = tmp_path / "export.json"
    salt = _reload_salt(src_path)
    with (
        derive_key(bytearray(passphrase), salt) as key,
        VaultConnection.open(src_path, key) as vc,
    ):
        export_json(vc, output_path=export_path, now_epoch=0)

    dst_path = tmp_path / "remapped.db"
    dst_salt = new_salt()
    save_salt(dst_path, dst_salt)
    with derive_key(bytearray(passphrase), dst_salt) as key:
        bootstrap(dst_path, key)
        with VaultConnection.open(dst_path, key) as vc:
            vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES ('01NEW', 'alice', 9)"
            )
        with VaultConnection.open(dst_path, key) as vc:
            import_json(vc, document_path=export_path, agent_external_id="01NEW")
        with VaultConnection.open(dst_path, key) as vc:
            agent_ids = {
                row[0]
                for row in vc.connection.execute(
                    "SELECT DISTINCT a.external_id FROM facets f JOIN agents a ON a.id = f.agent_id"
                ).fetchall()
            }
    assert agent_ids == {"01NEW"}


def _reload_salt(vault_path: Path) -> bytes:
    # Recover the salt a fresh derive_key call can consume.
    from tessera.vault.encryption import load_salt

    return load_salt(vault_path)
