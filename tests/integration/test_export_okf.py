"""OKF v0.1 vault export bundle."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.migration import bootstrap
from tessera.vault import audit_chain
from tessera.vault import capture as vault_capture
from tessera.vault import compiled as vault_compiled
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt
from tessera.vault.okf import OKF_VERSION, TESSERA_EXTERNAL_ID, export_okf, parse_concept

_RESERVED = {"index.md", "log.md"}
_LINK_RE = re.compile(r"\]\((/[^)]+\.md)\)")


@pytest.fixture
def okf_seeded_vault(open_vault: VaultConnection) -> VaultConnection:
    conn = open_vault.connection
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01AGTOKF', 'daisy', 1_000_000)"
    )
    agent_id = int(conn.execute("SELECT id FROM agents WHERE external_id='01AGTOKF'").fetchone()[0])

    identity = vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="identity",
        content="# Daisy\nPrimary operator profile.",
        source_tool="test",
        metadata={"name": "Daisy", "tags": ["person"]},
        captured_at=1_000_001,
    ).external_id
    preference = vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="preference",
        content="Prefer uv for Python packaging.",
        source_tool="test",
        metadata={"title": "Python package manager", "description": "Use uv by default."},
        captured_at=1_000_002,
    ).external_id
    vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="workflow",
        content="Run focused tests before broad gates.",
        source_tool="test",
        captured_at=1_000_003,
    )
    vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="style",
        content="Reserved slug content stays a concept.",
        source_tool="test",
        metadata={"title": "Index"},
        captured_at=1_000_004,
    )
    vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="soft-deleted OKF note",
        source_tool="test",
        captured_at=1_000_005,
    )
    conn.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 1_000_006 "
        "WHERE content = 'soft-deleted OKF note'"
    )
    vault_compiled.register_compiled_artifact(
        conn,
        agent_id=agent_id,
        content="# Playbook\nUse the operator profile and packaging preference.",
        source_facets=[identity, preference],
        compiler_version="test-compiler",
        source_tool="test",
        metadata={
            "field_provenance": {
                "summary": {
                    "source_facets": [identity, preference],
                    "source_refs": [
                        {"path": "docs/system-design.md", "section": "OKF", "line": 12}
                    ],
                }
            }
        },
        captured_at=1_000_006,
    )
    return open_vault


@pytest.mark.integration
def test_okf_export_writes_conformant_bundle(
    okf_seeded_vault: VaultConnection, tmp_path: Path
) -> None:
    out_dir = tmp_path / "okf"

    summary = export_okf(okf_seeded_vault, output_dir=out_dir, include_deleted=False, now_epoch=42)

    assert summary.format == "okf"
    assert summary.output_path == out_dir
    root = parse_concept((out_dir / "index.md").read_text(encoding="utf-8"))
    assert root.frontmatter["okf_version"] == OKF_VERSION

    concept_paths = [path for path in out_dir.rglob("*.md") if path.name not in _RESERVED]
    assert concept_paths
    for path in concept_paths:
        parsed = parse_concept(path.read_text(encoding="utf-8"))
        assert parsed.frontmatter["type"]
        assert parsed.frontmatter[TESSERA_EXTERNAL_ID]

    style_docs = [path for path in (out_dir / "style").glob("*.md") if path.name not in _RESERVED]
    assert len(style_docs) == 1
    assert style_docs[0].name.startswith("index-")
    assert "Reserved slug content stays a concept." in style_docs[0].read_text(encoding="utf-8")

    db_counts = {
        str(row[0]): int(row[1])
        for row in okf_seeded_vault.connection.execute(
            "SELECT facet_type, COUNT(*) FROM facets WHERE is_deleted = 0 GROUP BY facet_type"
        ).fetchall()
    }
    assert summary.facets_by_type == db_counts

    compiled_docs = [
        path for path in (out_dir / "compiled_notebook").glob("*.md") if path.name not in _RESERVED
    ]
    assert len(compiled_docs) == 1
    compiled_text = compiled_docs[0].read_text(encoding="utf-8")
    citation_links = _LINK_RE.findall(compiled_text)
    assert citation_links
    for link in citation_links:
        assert (out_dir / link.lstrip("/")).is_file()
    assert "docs/system-design.md" in compiled_text


@pytest.mark.integration
def test_okf_export_include_deleted_parity(
    okf_seeded_vault: VaultConnection, tmp_path: Path
) -> None:
    live_dir = tmp_path / "live"
    all_dir = tmp_path / "all"

    live = export_okf(okf_seeded_vault, output_dir=live_dir, include_deleted=False, now_epoch=42)
    all_rows = export_okf(okf_seeded_vault, output_dir=all_dir, include_deleted=True, now_epoch=42)

    assert all_rows.facets == live.facets + 1
    assert "soft-deleted OKF note" not in _bundle_text(live_dir)
    assert "soft-deleted OKF note" in _bundle_text(all_dir)


@pytest.mark.integration
def test_okf_export_audits_counts_only_and_chain_verifies(
    okf_seeded_vault: VaultConnection, tmp_path: Path
) -> None:
    export_okf(okf_seeded_vault, output_dir=tmp_path / "okf", include_deleted=True, now_epoch=84)

    rows = okf_seeded_vault.connection.execute(
        "SELECT payload FROM audit_log WHERE op = 'vault_exported_okf'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == '{"facet_count":6,"included_deleted":true,"scrubbed":false}'
    audit_chain.verify_chain(okf_seeded_vault.connection)


@pytest.mark.integration
def test_okf_export_scrub_redacts_content_metadata_and_paths(
    okf_seeded_vault: VaultConnection, tmp_path: Path
) -> None:
    secret = "sk-" + "0123456789abcdefghijABCDEFGHIJKL"
    agent_id = int(
        okf_seeded_vault.connection.execute(
            "SELECT id FROM agents WHERE external_id='01AGTOKF'"
        ).fetchone()[0]
    )
    vault_capture.capture(
        okf_seeded_vault.connection,
        agent_id=agent_id,
        facet_type="preference",
        content=f"Keep this credential out of shared bundles: {secret}",
        source_tool="test",
        metadata={
            "name": f"Credential {secret}",
            "description": f"metadata secret {secret}",
            "tags": [f"tag-{secret}"],
        },
        captured_at=1_000_007,
    )

    raw_dir = tmp_path / "raw"
    scrubbed_dir = tmp_path / "scrubbed"
    force_dir = tmp_path / "force"
    export_okf(okf_seeded_vault, output_dir=raw_dir, now_epoch=90)
    export_okf(okf_seeded_vault, output_dir=scrubbed_dir, now_epoch=91, scrub=True)
    export_okf(okf_seeded_vault, output_dir=force_dir, now_epoch=92)
    export_okf(okf_seeded_vault, output_dir=force_dir, now_epoch=93, scrub=True, force=True)
    assert secret in _bundle_text(raw_dir)
    scrubbed = _bundle_text(scrubbed_dir)
    assert secret not in scrubbed
    assert "[REDACTED:openai_api_key]" in scrubbed
    assert all(
        secret not in str(path.relative_to(scrubbed_dir)) for path in scrubbed_dir.rglob("*")
    )
    forced = _bundle_text(force_dir)
    assert secret not in forced
    assert "[REDACTED:openai_api_key]" in forced
    assert all(secret not in str(path.relative_to(force_dir)) for path in force_dir.rglob("*"))


@pytest.mark.integration
def test_okf_export_refuses_non_empty_dir_without_force(
    okf_seeded_vault: VaultConnection, tmp_path: Path
) -> None:
    out_dir = tmp_path / "occupied"
    out_dir.mkdir()
    (out_dir / "unrelated.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty directory"):
        export_okf(okf_seeded_vault, output_dir=out_dir, now_epoch=100)

    summary = export_okf(okf_seeded_vault, output_dir=out_dir, now_epoch=101, force=True)

    assert summary.format == "okf"
    assert not (out_dir / "unrelated.txt").exists()
    assert (out_dir / "index.md").is_file()


@pytest.mark.integration
def test_cli_okf_warns_and_audit_verify_stays_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault_path = tmp_path / "vault.db"
    passphrase = "okf safety"
    _seed_cli_vault(vault_path, passphrase)
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase)
    parser = _build_parser()

    export_args = parser.parse_args(
        ["export", "--vault", str(vault_path), "--format", "okf", "--output", str(tmp_path / "okf")]
    )
    assert export_args.handler(export_args) == 0
    output = capsys.readouterr().out
    assert "WARN" in output
    assert "decrypted plaintext" in output

    verify_args = parser.parse_args(["audit", "verify", "--vault", str(vault_path)])
    assert verify_args.handler(verify_args) == 0


def _seed_cli_vault(vault_path: Path, passphrase: str) -> None:
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(passphrase.encode("utf-8")), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) "
                "VALUES ('01CLIOKF', 'cli', 1_000_000)"
            )
            agent_id = int(
                vc.connection.execute(
                    "SELECT id FROM agents WHERE external_id='01CLIOKF'"
                ).fetchone()[0]
            )
            vault_capture.capture(
                vc.connection,
                agent_id=agent_id,
                facet_type="identity",
                content="CLI OKF export identity.",
                source_tool="test",
                captured_at=1_000_001,
            )


def _bundle_text(path: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(path.rglob("*.md")))
