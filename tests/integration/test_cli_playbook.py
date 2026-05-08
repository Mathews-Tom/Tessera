"""End-to-end ``tessera playbook`` exercises against a real vault.

Bootstraps a vault, seeds a target descriptor and source facets, then
drives the CLI parser the same way the user would. Covers the five
Phase 5 subcommands: targets, sources, scaffold, register, stale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tessera.cli.__main__ import main as cli_main
from tessera.migration import bootstrap
from tessera.vault import compiled, facets
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

_PASSPHRASE = b"correct horse battery staple"


def _bootstrap_vault(path: Path, *, agent_name: str = "primary") -> None:
    """Create a fresh vault with one agent so CLI auto-resolution works.

    The CLI's ``resolve_agent_id`` auto-selects the single agent in
    the vault; pre-seeding an agent keeps the playbook subcommands
    free of ``--agent-id`` boilerplate in the integration scenarios.
    """

    salt = new_salt()
    salt_path = path.with_suffix(".db.salt")
    salt_path.write_bytes(salt)
    k = derive_key(bytearray(_PASSPHRASE), salt)
    bootstrap(path, k)
    with VaultConnection.open(path, k) as vc:
        vc.connection.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
            (f"01PLAYBOOK-{agent_name}", agent_name, 1),
        )
    k.wipe()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)
    return vault_path


def _open(vault_path: Path) -> VaultConnection:
    salt_path = vault_path.with_suffix(".db.salt")
    k = derive_key(bytearray(_PASSPHRASE), salt_path.read_bytes())
    return VaultConnection.open(vault_path, k)


def _seed_descriptor_and_source(vault_path: Path, *, target: str = "release_playbook") -> str:
    """Seed one descriptor + one source for ``target``; return the source id."""

    with _open(vault_path) as vc:
        agent_id = int(vc.connection.execute("SELECT id FROM agents LIMIT 1").fetchone()[0])
        facets.insert(
            vc.connection,
            agent_id=agent_id,
            facet_type="workflow",
            content=f"descriptor:{target}",
            source_tool="cli",
            metadata={
                "target": target,
                "task": "execute release prep consistently",
                "artifact_type": "playbook",
                "quality_bar": "catches every gating step",
                "expected_refresh": "manual",
            },
        )
        source_id, _ = facets.insert(
            vc.connection,
            agent_id=agent_id,
            facet_type="project",
            content=f"source for {target}",
            source_tool="cli",
            metadata={"compile_into": [target], "compile_role": "primary_source"},
        )
    return source_id


def _cli(args: list[str]) -> int:
    return cli_main(args)


def _passphrase_args(vault: Path) -> list[str]:
    return ["--vault", str(vault), "--passphrase", _PASSPHRASE.decode()]


# ---- targets ------------------------------------------------------------


@pytest.mark.integration
def test_playbook_targets_lists_descriptor_in_json(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_descriptor_and_source(vault)
    rc = _cli(["playbook", "targets", *_passphrase_args(vault), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["target"] == "release_playbook"
    assert payload[0]["artifact_type"] == "playbook"
    assert payload[0]["quality_bar"] == "catches every gating step"


@pytest.mark.integration
def test_playbook_targets_handles_empty_vault(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cli(["playbook", "targets", *_passphrase_args(vault), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == []


# ---- sources ------------------------------------------------------------


@pytest.mark.integration
def test_playbook_sources_lists_tagged_facets(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_id = _seed_descriptor_and_source(vault)
    rc = _cli(
        [
            "playbook",
            "sources",
            "release_playbook",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "release_playbook"
    sources: list[dict[str, Any]] = payload["sources"]
    assert len(sources) == 1
    assert sources[0]["external_id"] == source_id
    assert sources[0]["facet_type"] == "project"
    assert sources[0]["metadata"]["compile_role"] == "primary_source"


# ---- scaffold -----------------------------------------------------------


@pytest.mark.integration
def test_playbook_scaffold_writes_markdown(vault: Path, tmp_path: Path) -> None:
    _seed_descriptor_and_source(vault)
    out_path = tmp_path / "scaffold.md"
    rc = _cli(
        [
            "playbook",
            "scaffold",
            "release_playbook",
            "--out",
            str(out_path),
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    body = out_path.read_text(encoding="utf-8")
    # Stable section headings — the contract is that an external
    # compiler can rely on these being present.
    assert "# Compile brief: release_playbook" in body
    assert "## Task" in body
    assert "## Quality bar" in body
    assert "## Source facets" in body
    assert "## Required output sections" in body
    assert "## Provenance expectations" in body
    assert "## Eval questions" in body
    # Descriptor metadata bleeds into the brief.
    assert "execute release prep consistently" in body
    assert "catches every gating step" in body


@pytest.mark.integration
def test_playbook_scaffold_refuses_to_overwrite(vault: Path, tmp_path: Path) -> None:
    _seed_descriptor_and_source(vault)
    out_path = tmp_path / "scaffold.md"
    out_path.write_text("existing content", encoding="utf-8")
    rc = _cli(
        [
            "playbook",
            "scaffold",
            "release_playbook",
            "--out",
            str(out_path),
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1
    assert out_path.read_text(encoding="utf-8") == "existing content"


@pytest.mark.integration
def test_playbook_scaffold_force_overwrites(vault: Path, tmp_path: Path) -> None:
    _seed_descriptor_and_source(vault)
    out_path = tmp_path / "scaffold.md"
    out_path.write_text("stale", encoding="utf-8")
    rc = _cli(
        [
            "playbook",
            "scaffold",
            "release_playbook",
            "--out",
            str(out_path),
            "--force",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    body = out_path.read_text(encoding="utf-8")
    assert body.startswith("# Compile brief: release_playbook")


@pytest.mark.integration
def test_playbook_scaffold_unknown_target_fails(vault: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "scaffold.md"
    rc = _cli(
        [
            "playbook",
            "scaffold",
            "absent_target",
            "--out",
            str(out_path),
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1
    assert not out_path.exists()


# ---- register -----------------------------------------------------------


@pytest.mark.integration
def test_playbook_register_writes_artifact_with_default_sources(
    vault: Path, tmp_path: Path
) -> None:
    source_id = _seed_descriptor_and_source(vault)
    body_path = tmp_path / "playbook.md"
    body_path.write_text("# release playbook\n\nSynthesised body.", encoding="utf-8")
    rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    with _open(vault) as vc:
        agent_id = int(vc.connection.execute("SELECT id FROM agents LIMIT 1").fetchone()[0])
        artifacts = compiled.list_for_agent(vc.connection, agent_id=agent_id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.compiler_version == "cc/release-recipe@1"
    assert artifact.artifact_type == "playbook"
    assert artifact.source_facets == (source_id,)
    assert "Synthesised body" in artifact.content


@pytest.mark.integration
def test_playbook_register_explicit_source_id_overrides_default(
    vault: Path, tmp_path: Path
) -> None:
    source_id = _seed_descriptor_and_source(vault)
    body_path = tmp_path / "playbook.md"
    body_path.write_text("body", encoding="utf-8")
    rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            "--source-id",
            source_id,
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0


@pytest.mark.integration
def test_playbook_register_fails_when_no_sources(vault: Path, tmp_path: Path) -> None:
    """Without any compile_into-tagged source, register refuses loudly.

    Falling back to "register an artifact with no sources" would
    bypass the ADR 0019 source-list contract; the CLI must surface
    a CliError instead.
    """

    body_path = tmp_path / "playbook.md"
    body_path.write_text("body", encoding="utf-8")
    rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_register_fails_on_empty_content(vault: Path, tmp_path: Path) -> None:
    _seed_descriptor_and_source(vault)
    body_path = tmp_path / "empty.md"
    body_path.write_text("   \n", encoding="utf-8")
    rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1


# ---- stale --------------------------------------------------------------


@pytest.mark.integration
def test_playbook_stale_emits_cause_in_json(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_id = _seed_descriptor_and_source(vault)
    body_path = tmp_path / "playbook.md"
    body_path.write_text("body", encoding="utf-8")
    register_rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            *_passphrase_args(vault),
        ]
    )
    assert register_rc == 0
    capsys.readouterr()  # discard register stdout
    # Soft-delete the source through the canonical path so the
    # cascade fires through ``mark_stale_for_source``.
    with _open(vault) as vc:
        flipped = facets.soft_delete(vc.connection, source_id)
    assert flipped is True
    rc = _cli(["playbook", "stale", *_passphrase_args(vault), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    record = payload[0]
    assert record["last_source_external_id"] == source_id
    assert record["last_source_op"] == "facet_soft_deleted"


@pytest.mark.integration
def test_playbook_stale_returns_empty_for_fresh_vault(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cli(["playbook", "stale", *_passphrase_args(vault), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


# ---- no subcommand ------------------------------------------------------


@pytest.mark.integration
def test_playbook_with_no_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _cli(["playbook"])
    assert rc == 2
    captured = capsys.readouterr()
    # Help goes through argparse's print_help — accept either stream.
    assert "Subcommands" in captured.out + captured.err


# ---- TTY rendering branches --------------------------------------------
#
# The Rich console reports ``is_terminal=False`` under pytest, which sends
# every subcommand down the JSON branch. These tests force the TTY path
# via monkeypatch so the table-rendering branches are exercised end-to-end.


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the CLI is attached to a wide TTY so the table branches run.

    Rich's ``Console.is_terminal`` is a property derived from
    ``_force_terminal`` (when set) and ``file.isatty()`` otherwise.
    Setting ``_force_terminal=True`` is the documented override.
    Width is bumped so column truncation does not eat the cell values
    the test asserts on.
    """

    from tessera.cli import _ui

    monkeypatch.setattr(_ui.console, "_force_terminal", True, raising=False)
    monkeypatch.setattr(_ui.console, "_width", 200, raising=False)
    monkeypatch.setattr(_ui.err_console, "_force_terminal", True, raising=False)
    monkeypatch.setattr(_ui.err_console, "_width", 200, raising=False)


@pytest.mark.integration
def test_playbook_targets_renders_table(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_descriptor_and_source(vault)
    _force_tty(monkeypatch)
    rc = _cli(["playbook", "targets", *_passphrase_args(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "release_playbook" in out


@pytest.mark.integration
def test_playbook_targets_empty_table_branch(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_tty(monkeypatch)
    rc = _cli(["playbook", "targets", *_passphrase_args(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no compile target descriptors found" in out


@pytest.mark.integration
def test_playbook_sources_renders_table(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_descriptor_and_source(vault)
    _force_tty(monkeypatch)
    rc = _cli(["playbook", "sources", "release_playbook", *_passphrase_args(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "release_playbook" in out
    assert "primary_source" in out


@pytest.mark.integration
def test_playbook_sources_empty_table_branch(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_tty(monkeypatch)
    rc = _cli(["playbook", "sources", "absent_target", *_passphrase_args(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no sources tagged" in out


@pytest.mark.integration
def test_playbook_stale_renders_table(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_id = _seed_descriptor_and_source(vault)
    body_path = tmp_path / "body.md"
    body_path.write_text("body", encoding="utf-8")
    register_rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            *_passphrase_args(vault),
        ]
    )
    assert register_rc == 0
    capsys.readouterr()
    with _open(vault) as vc:
        facets.soft_delete(vc.connection, source_id)
    _force_tty(monkeypatch)
    rc = _cli(["playbook", "stale", *_passphrase_args(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "facet_soft_deleted" in out


@pytest.mark.integration
def test_playbook_stale_empty_table_branch(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_tty(monkeypatch)
    rc = _cli(["playbook", "stale", *_passphrase_args(vault)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no stale compiled artifacts" in out


# ---- error paths --------------------------------------------------------


@pytest.mark.integration
def test_playbook_register_missing_content_file(vault: Path, tmp_path: Path) -> None:
    rc = _cli(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(tmp_path / "does_not_exist.md"),
            "--compiler-version",
            "cc/release-recipe@1",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_scaffold_no_descriptor_with_sources_warns(vault: Path, tmp_path: Path) -> None:
    """Scaffold succeeds when sources exist but no descriptor is registered.

    The plan permits a target to live without a descriptor while the
    user is still drafting the contract — sources are the operational
    membership, the descriptor is the named context. The CLI warns so
    the user knows the brief used placeholder task/quality_bar lines.
    """

    # Seed only sources, no descriptor.
    with _open(vault) as vc:
        agent_id = int(vc.connection.execute("SELECT id FROM agents LIMIT 1").fetchone()[0])
        facets.insert(
            vc.connection,
            agent_id=agent_id,
            facet_type="project",
            content="lone source",
            source_tool="cli",
            metadata={"compile_into": ["draft_target"]},
        )
    out_path = tmp_path / "scaffold.md"
    rc = _cli(
        [
            "playbook",
            "scaffold",
            "draft_target",
            "--out",
            str(out_path),
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    body = out_path.read_text(encoding="utf-8")
    assert "TODO: describe the recurring task" in body
    assert "TODO: describe the caller's acceptance criterion" in body


# ---- inspect ------------------------------------------------------------
#
# Phase 7 of the V0.5 compiled-Playbooks plan: a narrow read surface for
# already-registered artifacts. The fixtures below cover the four
# resolution paths (target → fresh, target → stale fallback, ULID, missing)
# and the three field-shape behaviours (no --field, single --field,
# missing --field) plus the --require-fresh and --provenance flags.


_ARTIFACT_BODY = (
    "# release playbook\n"
    "\n"
    "## Retrieval pipeline\n"
    "\n"
    "Recall hydrates each match with mode and is_stale via a single LEFT JOIN.\n"
    "\n"
    "## Staleness policy\n"
    "\n"
    "Stale artifacts surface but never authoritative; recompile or route to raw.\n"
    "\n"
    "### Edge cases\n"
    "\n"
    "Cross-agent membership cannot cascade.\n"
)

_ARTIFACT_PROVENANCE: dict[str, dict[str, Any]] = {
    "Retrieval pipeline": {
        "source_facets": [],
        "source_refs": [{"path": "docs/swcr-spec.md", "ref_kind": "supports"}],
        "confidence": "high",
    }
}


def _register_inspect_artifact(
    vault_path: Path,
    *,
    target: str = "release_playbook",
    body: str = _ARTIFACT_BODY,
    field_provenance: dict[str, dict[str, Any]] | None = None,
    compiler_version: str = "claude-code/release-recipe@2026-05-09",
) -> tuple[str, str]:
    """Seed a descriptor + source + registered artifact and return ids.

    Returns ``(source_external_id, artifact_external_id)``. Mirrors the
    existing scaffold/register-test seed pattern but registers through
    the storage layer directly so the inspect tests read from a
    populated artifact without depending on the register subcommand's
    side effects.
    """

    source_id = _seed_descriptor_and_source(vault_path, target=target)
    metadata: dict[str, Any] = {}
    if field_provenance is not None:
        metadata["field_provenance"] = field_provenance
    with _open(vault_path) as vc:
        agent_id = int(vc.connection.execute("SELECT id FROM agents LIMIT 1").fetchone()[0])
        artifact_id = compiled.register_compiled_artifact(
            vc.connection,
            agent_id=agent_id,
            content=body,
            source_facets=[source_id],
            artifact_type="playbook",
            compiler_version=compiler_version,
            source_tool="cli",
            metadata=metadata or None,
        )
    return source_id, artifact_id


@pytest.mark.integration
def test_playbook_inspect_resolves_target_to_latest_fresh(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, artifact_id = _register_inspect_artifact(vault, field_provenance=_ARTIFACT_PROVENANCE)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["external_id"] == artifact_id
    assert payload["resolved_target"] == "release_playbook"
    assert payload["is_stale"] is False
    # Without --field the inspect emits the artifact summary plus the
    # bounded full-content snippet so a piped reader still sees the
    # body without exploding the field tree.
    assert payload["fields"] == []
    assert "release playbook" in payload["content"]


@pytest.mark.integration
def test_playbook_inspect_resolves_ulid_lookup(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, artifact_id = _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            artifact_id,
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["external_id"] == artifact_id
    # ULID lookup does not infer a target name — Phase 7 keeps the
    # inspect surface honest about what it knows.
    assert payload["resolved_target"] is None


@pytest.mark.integration
def test_playbook_inspect_unknown_target_fails(vault: Path) -> None:
    rc = _cli(
        [
            "playbook",
            "inspect",
            "nonexistent_target",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_unknown_ulid_fails(vault: Path) -> None:
    # 26 chars in the ULID alphabet but unallocated.
    rc = _cli(
        [
            "playbook",
            "inspect",
            "01ABCDEFGHJKMNPQRSTVWXYZ00",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_field_returns_section_snippet(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_inspect_artifact(vault, field_provenance=_ARTIFACT_PROVENANCE)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Retrieval pipeline",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    fields: list[dict[str, Any]] = payload["fields"]
    assert len(fields) == 1
    assert fields[0]["name"] == "Retrieval pipeline"
    assert fields[0]["section_heading"] == "Retrieval pipeline"
    assert "LEFT JOIN" in fields[0]["snippet"]
    assert fields[0]["provenance"] is None
    # No --provenance flag so the artifact-level provenance map stays
    # off the response shape.
    assert "field_provenance" not in payload


@pytest.mark.integration
def test_playbook_inspect_field_with_provenance_attaches_entry(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_inspect_artifact(vault, field_provenance=_ARTIFACT_PROVENANCE)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "retrieval pipeline",  # case-insensitive match
            "--provenance",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    fields: list[dict[str, Any]] = json.loads(capsys.readouterr().out)["fields"]
    assert fields[0]["provenance"] is not None
    assert fields[0]["provenance"]["confidence"] == "high"


@pytest.mark.integration
def test_playbook_inspect_multiple_fields(vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Retrieval pipeline",
            "--field",
            "Staleness policy",
            "--field",
            "Edge cases",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    fields: list[dict[str, Any]] = json.loads(capsys.readouterr().out)["fields"]
    names = [fv["name"] for fv in fields]
    assert names == ["Retrieval pipeline", "Staleness policy", "Edge cases"]
    # ### headings still index into _extract_sections.
    edge = next(fv for fv in fields if fv["name"] == "Edge cases")
    assert "cross-agent" in edge["snippet"].lower()


@pytest.mark.integration
def test_playbook_inspect_missing_field_fails_loudly(vault: Path) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Definitely not a section",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_provenance_only_field(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field that exists only in field_provenance must still resolve.

    Phase 7's contract treats markdown headings and provenance keys as
    co-equal field surfaces; the user does not need to know whether a
    given key is keyed off prose or metadata.
    """

    extra_prov: dict[str, dict[str, Any]] = {
        "Caller-side eval summary": {
            "source_facets": [],
            "notes": "metadata-only entry",
        }
    }
    _register_inspect_artifact(vault, field_provenance=extra_prov)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Caller-side eval summary",
            "--provenance",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    fields: list[dict[str, Any]] = json.loads(capsys.readouterr().out)["fields"]
    assert fields[0]["section_heading"] is None
    assert fields[0]["snippet"] is None
    assert fields[0]["provenance"]["notes"] == "metadata-only entry"


@pytest.mark.integration
def test_playbook_inspect_require_fresh_fails_on_stale(vault: Path) -> None:
    source_id, _ = _register_inspect_artifact(vault)
    # Soft-delete the source so the artifact flips to is_stale=1.
    with _open(vault) as vc:
        assert facets.soft_delete(vc.connection, source_id) is True
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--require-fresh",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_stale_target_falls_back_when_fresh_missing(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --require-fresh, a stale-only target still resolves.

    The inspect surface keeps stale artifacts visible per the V0.5-P7
    retrieval contract; it just refuses to call them fresh and emits
    the cascade cause so the caller can decide whether to recompile.
    """

    source_id, artifact_id = _register_inspect_artifact(vault)
    with _open(vault) as vc:
        facets.soft_delete(vc.connection, source_id)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["external_id"] == artifact_id
    assert payload["is_stale"] is True
    assert payload["stale_cause"]["source_op"] == "facet_soft_deleted"


@pytest.mark.integration
def test_playbook_inspect_max_snippet_truncates(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Retrieval pipeline",
            "--max-snippet",
            "20",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    fields: list[dict[str, Any]] = json.loads(capsys.readouterr().out)["fields"]
    assert fields[0]["snippet_truncated"] is True
    assert len(fields[0]["snippet"]) == 20


@pytest.mark.integration
def test_playbook_inspect_max_snippet_zero_returns_full_content(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--max-snippet",
            "0",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["content_truncated"] is False
    assert payload["content"] == _ARTIFACT_BODY


@pytest.mark.integration
def test_playbook_inspect_max_snippet_negative_fails(vault: Path) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--max-snippet",
            "-1",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_target_without_sources_fails(vault: Path) -> None:
    rc = _cli(
        [
            "playbook",
            "inspect",
            "no_such_target",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_target_with_sources_no_artifact_fails(vault: Path) -> None:
    """Target has tagged sources but no compiled artifact yet.

    The inspect surface refuses rather than fabricating an empty view —
    Phase 7's no-fallback constraint treats "the target exists but
    nothing was compiled" as a hard miss.
    """

    _seed_descriptor_and_source(vault, target="unbuilt_target")
    rc = _cli(
        [
            "playbook",
            "inspect",
            "unbuilt_target",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_provenance_summary_without_field(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--provenance without --field returns the full field_provenance map."""

    _register_inspect_artifact(vault, field_provenance=_ARTIFACT_PROVENANCE)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--provenance",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "Retrieval pipeline" in payload["field_provenance"]


@pytest.mark.integration
def test_playbook_inspect_renders_table_branch(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_inspect_artifact(vault)
    _force_tty(monkeypatch)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Retrieval pipeline",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Retrieval pipeline" in out
    assert "LEFT JOIN" in out


@pytest.mark.integration
def test_playbook_inspect_renders_table_with_full_content(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_inspect_artifact(vault)
    _force_tty(monkeypatch)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "release playbook" in out


@pytest.mark.integration
def test_playbook_inspect_renders_stale_warning(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_id, _ = _register_inspect_artifact(vault)
    with _open(vault) as vc:
        facets.soft_delete(vc.connection, source_id)
    _force_tty(monkeypatch)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    combined = capsys.readouterr()
    assert "stale" in (combined.out + combined.err).lower()


@pytest.mark.integration
def test_playbook_inspect_empty_field_value_fails(vault: Path) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "   ",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_playbook_inspect_duplicate_field_dedupes(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_inspect_artifact(vault)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--field",
            "Retrieval pipeline",
            "--field",
            "Retrieval pipeline",
            *_passphrase_args(vault),
            "--json",
        ]
    )
    assert rc == 0
    fields: list[dict[str, Any]] = json.loads(capsys.readouterr().out)["fields"]
    assert len(fields) == 1


@pytest.mark.integration
def test_playbook_inspect_renders_full_provenance_table(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_inspect_artifact(vault, field_provenance=_ARTIFACT_PROVENANCE)
    _force_tty(monkeypatch)
    rc = _cli(
        [
            "playbook",
            "inspect",
            "release_playbook",
            "--provenance",
            *_passphrase_args(vault),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "field_provenance" in out
    assert "Retrieval pipeline" in out
