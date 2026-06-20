"""OKF v0.1 importer round-trip + strict write-path (plan Phase 4)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlcipher3

from tessera.cli.__main__ import _build_parser
from tessera.migration import bootstrap
from tessera.vault import audit_chain
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt
from tessera.vault.okf import export_okf, import_okf

_PASSPHRASE = b"correct horse battery staple"

# Facets the round-trip exercises. Compiled-notebook concepts are an
# export-only projection (their provenance lives in a sibling table the
# importer does not reconstruct), so the lossless-identity tests stick to
# the free-form facet types that map one-to-one onto a facet row.
_SEED: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("identity", "# Daisy\nPrimary operator profile.", {"name": "Daisy", "tags": ["person"]}),
    ("preference", "Prefer uv for Python packaging.", {"title": "Packaging", "description": "uv."}),
    ("workflow", "Run focused tests before broad gates.", {}),
    ("project", "Anneal architecture overview.", {"title": "Anneal"}),
    ("style", "Terse, evidence-first prose.", {}),
)


def _seed_source(conn: sqlcipher3.Connection, *, agent_external_id: str = "01AGTSRC") -> int:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, 'daisy', 1_000_000)",
        (agent_external_id,),
    )
    agent_id = int(
        conn.execute(
            "SELECT id FROM agents WHERE external_id = ?", (agent_external_id,)
        ).fetchone()[0]
    )
    for index, (facet_type, content, metadata) in enumerate(_SEED):
        vault_capture.capture(
            conn,
            agent_id=agent_id,
            facet_type=facet_type,
            content=content,
            source_tool="test",
            metadata=metadata,
            captured_at=1_000_001 + index,
        )
    return agent_id


def _seed_soft_deleted(conn: sqlcipher3.Connection, agent_id: int) -> None:
    vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="soft-deleted OKF note",
        source_tool="test",
        captured_at=1_000_100,
    )
    conn.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 1_000_101 "
        "WHERE content = 'soft-deleted OKF note'"
    )


@contextlib.contextmanager
def _fresh_vault(vault_path: Path) -> Iterator[VaultConnection]:
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(_PASSPHRASE), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            yield vc


def _create_agent(conn: sqlcipher3.Connection, external_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 1_000_000)",
        (external_id, name),
    )


def _ids_by_type(conn: sqlcipher3.Connection) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT facet_type, external_id FROM facets WHERE is_deleted = 0"
    ).fetchall()
    out: dict[str, set[str]] = {}
    for facet_type, external_id in rows:
        out.setdefault(str(facet_type), set()).add(str(external_id))
    return out


def _facet_count(conn: sqlcipher3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM facets").fetchone()[0])


def _concept_doc(
    *,
    type_value: str | None = None,
    facet_type: str | None = None,
    external_id: str | None = None,
    body: str = "content",
    extra: tuple[str, ...] = (),
) -> str:
    lines = ["---"]
    if type_value is not None:
        lines.append(f"type: {json.dumps(type_value)}")
    if facet_type is not None:
        lines.append(f"tessera_facet_type: {json.dumps(facet_type)}")
    if external_id is not None:
        lines.append(f"tessera_external_id: {json.dumps(external_id)}")
    lines.extend(extra)
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Lossless-identity round-trip.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_round_trip_preserves_external_ids_and_counts(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    source = open_vault
    _seed_source(source.connection)
    source_ids = _ids_by_type(source.connection)

    bundle = tmp_path / "bundle"
    export_okf(source, output_dir=bundle, now_epoch=42, include_deleted=False)

    with _fresh_vault(tmp_path / "target.db") as target:
        _create_agent(target.connection, "01TARGET", "target")
        summary = import_okf(target, bundle_dir=bundle, agent_external_id="01TARGET")
        target_ids = _ids_by_type(target.connection)

    assert not summary.errors
    assert summary.skipped == 0
    assert target_ids == source_ids
    assert summary.facets_by_type == {t: len(ids) for t, ids in source_ids.items()}


@pytest.mark.integration
def test_reimport_dedups_with_no_duplicates(open_vault: VaultConnection, tmp_path: Path) -> None:
    source = open_vault
    _seed_source(source.connection)
    bundle = tmp_path / "bundle"
    export_okf(source, output_dir=bundle, now_epoch=42)

    before = _facet_count(source.connection)
    # Sole agent auto-resolves when agent_external_id is omitted.
    summary = import_okf(source, bundle_dir=bundle)
    after = _facet_count(source.connection)

    assert not summary.errors
    assert after == before  # content-hash dedup wrote no new rows
    assert summary.facets == len(_SEED)  # every concept resolved to a live facet


@pytest.mark.integration
def test_soft_deleted_round_trip_undeletes_on_reimport(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    source = open_vault
    agent_id = _seed_source(source.connection)
    _seed_soft_deleted(source.connection, agent_id)

    live_bundle = tmp_path / "live"
    export_okf(source, output_dir=live_bundle, now_epoch=42, include_deleted=False)
    all_bundle = tmp_path / "all"
    export_okf(source, output_dir=all_bundle, now_epoch=42, include_deleted=True)

    # The live bundle omits the tombstoned concept; the include-deleted one keeps it.
    assert "soft-deleted OKF note" not in _bundle_text(live_bundle)
    assert "soft-deleted OKF note" in _bundle_text(all_bundle)

    deleted_before = source.connection.execute(
        "SELECT is_deleted FROM facets WHERE content = 'soft-deleted OKF note'"
    ).fetchone()[0]
    assert int(deleted_before) == 1

    summary = import_okf(source, bundle_dir=all_bundle)
    assert not summary.errors

    deleted_after = source.connection.execute(
        "SELECT is_deleted FROM facets WHERE content = 'soft-deleted OKF note'"
    ).fetchone()[0]
    assert int(deleted_after) == 0  # re-import honored the round-trip via un-delete


# --------------------------------------------------------------------------
# Strict write-path: external bundles cannot bypass the allowlist or escape.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_unknown_type_collected_not_written(open_vault: VaultConnection, tmp_path: Path) -> None:
    target = open_vault
    _create_agent(target.connection, "01T", "t")
    bundle = tmp_path / "bundle"
    (bundle / "preference").mkdir(parents=True)
    (bundle / "mystery").mkdir(parents=True)
    (bundle / "preference" / "good.md").write_text(
        _concept_doc(
            type_value="Preference",
            facet_type="preference",
            external_id="01GOODPREF",
            body="keep me",
        ),
        encoding="utf-8",
    )
    (bundle / "mystery" / "weird.md").write_text(
        _concept_doc(
            type_value="Mystery Concept",
            facet_type="mystery",
            external_id="01WEIRD",
            body="reject me",
        ),
        encoding="utf-8",
    )

    summary = import_okf(target, bundle_dir=bundle, agent_external_id="01T")

    assert summary.facets_by_type == {"preference": 1}
    assert any("mystery" in line for line in summary.errors)
    assert _facet_count(target.connection) == 1  # the unknown type wrote nothing


@pytest.mark.integration
def test_missing_type_skipped_per_section_9(open_vault: VaultConnection, tmp_path: Path) -> None:
    target = open_vault
    _create_agent(target.connection, "01T", "t")
    bundle = tmp_path / "bundle"
    (bundle / "preference").mkdir(parents=True)
    (bundle / "preference" / "good.md").write_text(
        _concept_doc(
            type_value="Preference",
            facet_type="preference",
            external_id="01GOODPREF",
            body="keep me",
        ),
        encoding="utf-8",
    )
    # Frontmatter present but no `type` field -> §9 non-conformant for write.
    (bundle / "preference" / "notype.md").write_text(
        _concept_doc(external_id="01NOTYPE", extra=('title: "No type"',), body="skip me"),
        encoding="utf-8",
    )

    summary = import_okf(target, bundle_dir=bundle, agent_external_id="01T")

    assert summary.skipped == 1
    assert summary.facets_by_type == {"preference": 1}
    assert _facet_count(target.connection) == 1


@pytest.mark.integration
def test_path_traversal_entry_rejected_at_boundary(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    target = open_vault
    _create_agent(target.connection, "01T", "t")
    bundle = tmp_path / "bundle"
    (bundle / "preference").mkdir(parents=True)
    (bundle / "preference" / "good.md").write_text(
        _concept_doc(
            type_value="Preference",
            facet_type="preference",
            external_id="01GOODPREF",
            body="keep me",
        ),
        encoding="utf-8",
    )
    # A concept symlinked to a file outside the bundle root.
    outside = tmp_path / "outside.md"
    outside.write_text(
        _concept_doc(
            type_value="Preference",
            facet_type="preference",
            external_id="01ESCAPE",
            body="exfiltrate me",
        ),
        encoding="utf-8",
    )
    (bundle / "preference" / "evil.md").symlink_to(outside)

    summary = import_okf(target, bundle_dir=bundle, agent_external_id="01T")

    assert any("escapes" in line for line in summary.errors)
    assert summary.facets_by_type == {"preference": 1}
    assert _facet_count(target.connection) == 1


# --------------------------------------------------------------------------
# Audit chain stays intact and import rides it.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_import_rides_audit_chain_and_verify_stays_green(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    source = open_vault
    _seed_source(source.connection)
    bundle = tmp_path / "bundle"
    export_okf(source, output_dir=bundle, now_epoch=42)

    with _fresh_vault(tmp_path / "target.db") as target:
        _create_agent(target.connection, "01TARGET", "target")
        import_okf(target, bundle_dir=bundle, agent_external_id="01TARGET")
        inserted = int(
            target.connection.execute(
                "SELECT COUNT(*) FROM audit_log WHERE op = 'facet_inserted'"
            ).fetchone()[0]
        )
        assert inserted == len(_SEED)
        # Raises AuditChainBrokenError if the chain is broken.
        audit_chain.verify_chain(target.connection)


@pytest.mark.integration
def test_cli_import_okf_round_trip_audit_verify_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault_path = tmp_path / "vault.db"
    passphrase = "okf import"
    with _cli_vault(vault_path, passphrase) as conn:
        _seed_source(conn, agent_external_id="01CLISRC")
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase)
    parser = _build_parser()
    bundle = tmp_path / "okf"

    export_args = parser.parse_args(
        ["export", "--vault", str(vault_path), "--format", "okf", "--output", str(bundle)]
    )
    assert export_args.handler(export_args) == 0
    capsys.readouterr()

    import_args = parser.parse_args(
        ["import-okf", "--vault", str(vault_path), "--input", str(bundle)]
    )
    assert import_args.handler(import_args) == 0

    verify_args = parser.parse_args(["audit", "verify", "--vault", str(vault_path)])
    assert verify_args.handler(verify_args) == 0


@contextlib.contextmanager
def _cli_vault(vault_path: Path, passphrase: str) -> Iterator[sqlcipher3.Connection]:
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(passphrase.encode("utf-8")), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            yield vc.connection


def _bundle_text(path: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(path.rglob("*.md")))
