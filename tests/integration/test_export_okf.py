"""OKF v0.1 vault export bundle."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tessera.vault import capture as vault_capture
from tessera.vault import compiled as vault_compiled
from tessera.vault.connection import VaultConnection
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


def _bundle_text(path: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(path.rglob("*.md")))
