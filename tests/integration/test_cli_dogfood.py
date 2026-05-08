"""End-to-end ``tessera dogfood`` exercise plus auto-emission hooks.

Round-trips the four subcommands (init/record/render/status) against
a per-test ledger directory and proves that ``tessera audit verify``,
``tessera playbook register``, and ``tessera playbook stale`` auto-
emit rows to every active gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.cli.__main__ import main as cli_main
from tessera.dogfood.ledger import Ledger
from tessera.dogfood.render import (
    EVIDENCE_END_MARKER,
    EVIDENCE_START_MARKER,
    SUMMARY_END_MARKER,
    SUMMARY_START_MARKER,
)
from tessera.migration import bootstrap
from tessera.vault import facets
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

_PASSPHRASE = b"correct horse battery staple"


@pytest.fixture
def ledger_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test ledger directory; isolates from the user's real ledger."""

    monkeypatch.setenv("TESSERA_DOGFOOD_DIR", str(tmp_path / "dogfood"))
    monkeypatch.delenv("TESSERA_DOGFOOD_DISABLE", raising=False)
    return tmp_path / "dogfood"


def _bootstrap_vault(path: Path) -> None:
    salt = new_salt()
    salt_path = path.with_suffix(".db.salt")
    salt_path.write_bytes(salt)
    k = derive_key(bytearray(_PASSPHRASE), salt)
    bootstrap(path, k)
    k.wipe()


def _bootstrap_vault_with_agent(path: Path) -> None:
    """Bootstrap a vault and seed one agent so CLI auto-resolution works.

    The playbook subcommands rely on ``resolve_agent_id``'s
    "auto-select the only agent" behaviour; pre-seeding one keeps the
    test argv free of ``--agent-id`` boilerplate.
    """

    salt = new_salt()
    salt_path = path.with_suffix(".db.salt")
    salt_path.write_bytes(salt)
    k = derive_key(bytearray(_PASSPHRASE), salt)
    bootstrap(path, k)
    with VaultConnection.open(path, k) as vc:
        vc.connection.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
            ("01PLAYBOOK-DOGFOOD-AGT", "primary", 1),
        )
    k.wipe()


def _seed_descriptor_and_source(vault_path: Path, *, target: str) -> str:
    """Seed one descriptor + one source facet for ``target``; return source id."""

    salt = vault_path.with_suffix(".db.salt").read_bytes()
    k = derive_key(bytearray(_PASSPHRASE), salt)
    with VaultConnection.open(vault_path, k) as vc:
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
    k.wipe()
    return source_id


@pytest.mark.integration
def test_dogfood_init_creates_gate_initialized_row(ledger_home: Path) -> None:
    """``init`` writes a gate_initialized row pinning operator + start-date."""

    rc = cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
            "--field",
            "machine_a=macbook.local",
            "--field",
            "machine_b=linux.local",
        ]
    )
    assert rc == 0
    ledger = Ledger("sync", base_dir=ledger_home)
    assert ledger.path.exists()
    rows = list(ledger.iter_records())
    assert len(rows) == 1
    init = rows[0]
    assert init.kind == "gate_initialized"
    assert init.payload["operator"] == "Tom"
    assert init.payload["start_date"] == "2026-05-09"
    assert init.payload["machine_a"] == "macbook.local"


@pytest.mark.integration
def test_dogfood_init_refuses_when_already_active(ledger_home: Path) -> None:
    """init twice without close raises the operator-fight guard."""

    args = [
        "dogfood",
        "init",
        "sync",
        "--operator",
        "Tom",
        "--start-date",
        "2026-05-09",
    ]
    assert cli_main(args) == 0
    rc = cli_main(args)
    assert rc == 1


@pytest.mark.integration
def test_dogfood_record_appends_typed_event(ledger_home: Path) -> None:
    """``record`` appends a typed row with auto-coerced int fields."""

    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    rc = cli_main(
        [
            "dogfood",
            "record",
            "sync",
            "--kind",
            "sync_op",
            "--field",
            "command=push",
            "--field",
            "exit_code=0",
            "--field",
            "elapsed_ms=1234",
        ]
    )
    assert rc == 0
    rows = list(Ledger("sync", base_dir=ledger_home).iter_records())
    assert len(rows) == 2
    op = rows[1]
    assert op.kind == "sync_op"
    assert op.payload == {"command": "push", "exit_code": 0, "elapsed_ms": 1234}


@pytest.mark.integration
def test_dogfood_record_rejects_kind_not_in_allowlist(ledger_home: Path) -> None:
    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    rc = cli_main(["dogfood", "record", "sync", "--kind", "recompile", "--field", "target=t"])
    assert rc == 1


@pytest.mark.integration
def test_dogfood_record_rejects_uninitialized_gate(ledger_home: Path) -> None:
    rc = cli_main(["dogfood", "record", "sync", "--kind", "note", "--field", "text=x"])
    assert rc == 1


@pytest.mark.integration
def test_dogfood_record_after_completion_only_admits_note(
    ledger_home: Path,
) -> None:
    """A completed gate accepts notes only — no further structured rows."""

    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    cli_main(
        [
            "dogfood",
            "record",
            "sync",
            "--kind",
            "gate_completed",
            "--field",
            "end_date=2026-06-12",
        ]
    )
    rc_op = cli_main(
        [
            "dogfood",
            "record",
            "sync",
            "--kind",
            "sync_op",
            "--field",
            "command=push",
            "--field",
            "exit_code=0",
            "--field",
            "elapsed_ms=1",
        ]
    )
    assert rc_op == 1
    rc_note = cli_main(
        ["dogfood", "record", "sync", "--kind", "note", "--field", "text=postmortem"]
    )
    assert rc_note == 0


@pytest.mark.integration
def test_dogfood_render_rewrites_doc_between_markers(ledger_home: Path, tmp_path: Path) -> None:
    """``render`` rewrites the doc's evidence-log + summary tables in place."""

    doc = tmp_path / "sync-dogfood.md"
    doc.write_text(
        "# Sync gate\n\n"
        "## Evidence log\n\n"
        f"{EVIDENCE_START_MARKER}\n"
        "old\n"
        f"{EVIDENCE_END_MARKER}\n\n"
        "## Acceptance summary\n\n"
        f"{SUMMARY_START_MARKER}\n"
        "old\n"
        f"{SUMMARY_END_MARKER}\n",
        encoding="utf-8",
    )
    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    cli_main(
        [
            "dogfood",
            "record",
            "sync",
            "--kind",
            "sync_op",
            "--field",
            "command=push",
            "--field",
            "exit_code=0",
            "--field",
            "elapsed_ms=1234",
        ]
    )
    cli_main(
        [
            "dogfood",
            "record",
            "sync",
            "--kind",
            "sync_op",
            "--field",
            "command=pull",
            "--field",
            "exit_code=0",
            "--field",
            "elapsed_ms=2345",
        ]
    )
    rc = cli_main(["dogfood", "render", "sync", "--doc", str(doc)])
    assert rc == 0
    text = doc.read_text(encoding="utf-8")
    # Markers preserved.
    assert EVIDENCE_START_MARKER in text
    assert EVIDENCE_END_MARKER in text
    assert SUMMARY_START_MARKER in text
    assert SUMMARY_END_MARKER in text
    # Old content gone.
    assert "old" not in text
    # Both push and pull appear in the evidence-log section.
    evidence_section = text.split(EVIDENCE_START_MARKER, 1)[1].split(EVIDENCE_END_MARKER, 1)[0]
    assert "push" in evidence_section
    assert "pull" in evidence_section
    # Acceptance summary reflects predicate evaluations.
    summary_section = text.split(SUMMARY_START_MARKER, 1)[1].split(SUMMARY_END_MARKER, 1)[0]
    assert "Push and pull both exercised" in summary_section
    assert "Met" in summary_section  # at least one predicate fires


@pytest.mark.integration
def test_dogfood_render_no_write_prints_without_touching_file(
    ledger_home: Path, tmp_path: Path
) -> None:
    """``--no-write`` prints to stdout but does not modify the doc."""

    doc = tmp_path / "sync-dogfood.md"
    body = (
        "# Sync gate\n\n"
        f"{EVIDENCE_START_MARKER}\n"
        "untouched\n"
        f"{EVIDENCE_END_MARKER}\n\n"
        f"{SUMMARY_START_MARKER}\n"
        "untouched\n"
        f"{SUMMARY_END_MARKER}\n"
    )
    doc.write_text(body, encoding="utf-8")
    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    rc = cli_main(["dogfood", "render", "sync", "--doc", str(doc), "--no-write"])
    assert rc == 0
    assert doc.read_text(encoding="utf-8") == body


@pytest.mark.integration
def test_dogfood_render_fails_loud_on_missing_markers(ledger_home: Path, tmp_path: Path) -> None:
    """A doc without markers must surface a MarkerError, not silently succeed."""

    doc = tmp_path / "no-markers.md"
    doc.write_text("# nothing here\n", encoding="utf-8")
    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    rc = cli_main(["dogfood", "render", "sync", "--doc", str(doc)])
    assert rc == 1


@pytest.mark.integration
def test_dogfood_status_reports_per_gate_state(ledger_home: Path) -> None:
    """``status`` prints one row per gate."""

    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    rc = cli_main(["dogfood", "status"])
    assert rc == 0
    rc_one = cli_main(["dogfood", "status", "sync"])
    assert rc_one == 0


@pytest.mark.integration
def test_dogfood_init_blocked_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    ledger_home: Path,  # noqa: ARG001 — fixture sets TESSERA_DOGFOOD_DIR
) -> None:
    """A disabled environment refuses ``dogfood init`` so the operator
    is not split between two ledgers."""

    monkeypatch.setenv("TESSERA_DOGFOOD_DISABLE", "1")
    rc = cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    assert rc == 1


# ---- audit verify auto-emission ----------------------------------------


@pytest.mark.integration
def test_audit_verify_auto_emits_to_active_gate(
    ledger_home: Path, tmp_path: Path
) -> None:
    """``tessera audit verify`` writes an audit_verify row to every active gate."""

    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)
    rc = cli_main(
        [
            "audit",
            "verify",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0
    rows = list(Ledger("sync", base_dir=ledger_home).iter_records())
    audit_rows = [r for r in rows if r.kind == "audit_verify"]
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["exit_code"] == 0
    assert audit_rows[0].payload["outcome"] in {"intact", "empty_chain"}


@pytest.mark.integration
def test_audit_verify_does_not_emit_when_no_gate_active(
    ledger_home: Path, tmp_path: Path
) -> None:
    """No active gate → no auto-emission, even with the env-var unset."""

    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)
    rc = cli_main(
        [
            "audit",
            "verify",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0
    sync_path = ledger_home / "sync.jsonl"
    assert not sync_path.exists()


@pytest.mark.integration
def test_audit_verify_skips_emission_when_disabled(
    ledger_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TESSERA_DOGFOOD_DISABLE=1`` suppresses emission even on an active gate."""

    cli_main(
        [
            "dogfood",
            "init",
            "sync",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    monkeypatch.setenv("TESSERA_DOGFOOD_DISABLE", "1")
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)
    rc = cli_main(
        [
            "audit",
            "verify",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0
    rows = list(Ledger("sync", base_dir=ledger_home).iter_records())
    assert all(r.kind != "audit_verify" for r in rows)


# ---- playbook auto-emission --------------------------------------------


@pytest.mark.integration
def test_playbook_register_emits_register_row_to_active_gate(
    ledger_home: Path, tmp_path: Path
) -> None:
    """``tessera playbook register`` writes a register row to active gates.

    Both the playbook gate and the compiled-notebook gate admit the
    ``register`` kind; ``auto_record`` dispatches to whichever is
    active. With only the playbook gate open, exactly one row lands.
    """

    cli_main(
        [
            "dogfood",
            "init",
            "playbook",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault_with_agent(vault_path)
    _seed_descriptor_and_source(vault_path, target="release_playbook")
    body_path = tmp_path / "playbook.md"
    body_path.write_text("# release\n\nbody", encoding="utf-8")
    rc = cli_main(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0
    rows = list(Ledger("playbook", base_dir=ledger_home).iter_records())
    register_rows = [r for r in rows if r.kind == "register"]
    assert len(register_rows) == 1
    payload = register_rows[0].payload
    assert payload["target"] == "release_playbook"
    assert payload["compiler_version"] == "cc/release-recipe@1"
    assert payload["exit_code"] == 0
    assert payload["source_count"] == 1
    assert len(payload["external_id"]) > 0
    # No row should have leaked into the compiled gate (uninitialized).
    compiled_path = ledger_home / "compiled.jsonl"
    assert not compiled_path.exists()


@pytest.mark.integration
def test_playbook_register_emits_to_both_gates_when_both_active(
    ledger_home: Path, tmp_path: Path
) -> None:
    """Active compiled + playbook gates both receive the register row."""

    for gate in ("playbook", "compiled"):
        cli_main(
            [
                "dogfood",
                "init",
                gate,
                "--operator",
                "Tom",
                "--start-date",
                "2026-05-09",
            ]
        )
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault_with_agent(vault_path)
    _seed_descriptor_and_source(vault_path, target="release_playbook")
    body_path = tmp_path / "playbook.md"
    body_path.write_text("body", encoding="utf-8")
    cli_main(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    pb_register = [
        r for r in Ledger("playbook", base_dir=ledger_home).iter_records() if r.kind == "register"
    ]
    cn_register = [
        r for r in Ledger("compiled", base_dir=ledger_home).iter_records() if r.kind == "register"
    ]
    assert len(pb_register) == 1
    assert len(cn_register) == 1
    assert pb_register[0].payload["external_id"] == cn_register[0].payload["external_id"]


@pytest.mark.integration
def test_playbook_register_emits_failure_row_with_error_class(
    ledger_home: Path, tmp_path: Path
) -> None:
    """A failed register still produces a ledger row carrying error_class."""

    cli_main(
        [
            "dogfood",
            "init",
            "playbook",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault_with_agent(vault_path)
    # Seed only a descriptor — no compile_into-tagged source — so register
    # fails with InvalidCompiledArtifactError after the source-resolve step.
    salt = vault_path.with_suffix(".db.salt").read_bytes()
    k = derive_key(bytearray(_PASSPHRASE), salt)
    with VaultConnection.open(vault_path, k) as vc:
        agent_id = int(vc.connection.execute("SELECT id FROM agents LIMIT 1").fetchone()[0])
        facets.insert(
            vc.connection,
            agent_id=agent_id,
            facet_type="workflow",
            content="descriptor:release_playbook",
            source_tool="cli",
            metadata={
                "target": "release_playbook",
                "task": "execute release prep consistently",
                "artifact_type": "playbook",
                "quality_bar": "catches every gating step",
                "expected_refresh": "manual",
            },
        )
    k.wipe()
    body_path = tmp_path / "playbook.md"
    body_path.write_text("body", encoding="utf-8")
    rc = cli_main(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 1
    rows = list(Ledger("playbook", base_dir=ledger_home).iter_records())
    register_rows = [r for r in rows if r.kind == "register"]
    assert len(register_rows) == 1
    payload = register_rows[0].payload
    assert payload["exit_code"] == 1
    assert payload["error_class"]  # non-empty error class on failure
    assert payload["external_id"] == ""


@pytest.mark.integration
def test_playbook_stale_emits_event_when_listing_non_empty(
    ledger_home: Path, tmp_path: Path
) -> None:
    """``tessera playbook stale`` emits a stale_event row when drift exists."""

    cli_main(
        [
            "dogfood",
            "init",
            "playbook",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault_with_agent(vault_path)
    source_id = _seed_descriptor_and_source(vault_path, target="release_playbook")
    body_path = tmp_path / "playbook.md"
    body_path.write_text("body", encoding="utf-8")
    rc_register = cli_main(
        [
            "playbook",
            "register",
            "release_playbook",
            "--content",
            str(body_path),
            "--compiler-version",
            "cc/release-recipe@1",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc_register == 0
    # Soft-delete the source so mark_stale_for_source flips the artifact.
    salt = vault_path.with_suffix(".db.salt").read_bytes()
    k = derive_key(bytearray(_PASSPHRASE), salt)
    with VaultConnection.open(vault_path, k) as vc:
        facets.soft_delete(vc.connection, source_id)
    k.wipe()
    rc_stale = cli_main(
        [
            "playbook",
            "stale",
            "--json",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc_stale == 0
    stale_rows = [
        r
        for r in Ledger("playbook", base_dir=ledger_home).iter_records()
        if r.kind == "stale_event"
    ]
    assert len(stale_rows) == 1
    payload = stale_rows[0].payload
    assert payload["stale_count_after"] == 1
    assert payload["source_external_id"] == source_id
    assert payload["source_op"]  # cascade cause is non-empty


@pytest.mark.integration
def test_playbook_stale_emits_no_row_when_listing_empty(
    ledger_home: Path, tmp_path: Path
) -> None:
    """An empty stale listing must not pollute the ledger with no-op rows."""

    cli_main(
        [
            "dogfood",
            "init",
            "playbook",
            "--operator",
            "Tom",
            "--start-date",
            "2026-05-09",
        ]
    )
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault_with_agent(vault_path)
    rc = cli_main(
        [
            "playbook",
            "stale",
            "--json",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0
    rows = list(Ledger("playbook", base_dir=ledger_home).iter_records())
    assert all(r.kind != "stale_event" for r in rows)
