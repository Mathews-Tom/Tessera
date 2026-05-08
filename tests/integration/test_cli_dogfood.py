"""End-to-end ``tessera dogfood`` exercise.

Round-trips the four subcommands (init/record/render/status) against
a per-test ledger directory and proves the doc-rewrite contract for
the marker-block renderer.
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


@pytest.fixture
def ledger_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test ledger directory; isolates from the user's real ledger."""

    monkeypatch.setenv("TESSERA_DOGFOOD_DIR", str(tmp_path / "dogfood"))
    monkeypatch.delenv("TESSERA_DOGFOOD_DISABLE", raising=False)
    return tmp_path / "dogfood"


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
