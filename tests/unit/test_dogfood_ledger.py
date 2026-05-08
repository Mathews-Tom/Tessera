"""Unit tests for the dogfood JSONL ledger.

Append + read round-trip, lifecycle state computation, ledger-corruption
surfacing, ``auto_record`` dispatch by ``(active gate, admitted kind)``,
and the env-var disable contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.dogfood.ledger import (
    DogfoodEmissionError,
    Ledger,
    LedgerCorruptionError,
    auto_record,
    is_disabled,
    ledger_dir,
    ledger_path,
)
from tessera.dogfood.schemas import Record, SchemaError


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test ledger directory; isolates from the user's real ledger."""

    monkeypatch.setenv("TESSERA_DOGFOOD_DIR", str(tmp_path))
    monkeypatch.delenv("TESSERA_DOGFOOD_DISABLE", raising=False)
    return tmp_path


@pytest.mark.unit
def test_ledger_path_resolves_under_env_override(base_dir: Path) -> None:
    """``ledger_path`` honors ``$TESSERA_DOGFOOD_DIR``."""

    assert ledger_path("sync") == base_dir / "sync.jsonl"
    assert ledger_path("compiled") == base_dir / "compiled.jsonl"
    assert ledger_path("playbook") == base_dir / "playbook.jsonl"


@pytest.mark.unit
def test_ledger_path_rejects_unknown_gate(base_dir: Path) -> None:
    with pytest.raises(SchemaError, match="unknown gate"):
        ledger_path("nonsense")


@pytest.mark.unit
def test_ledger_dir_explicit_arg_wins_over_env(base_dir: Path, tmp_path: Path) -> None:
    """An explicit ``base_dir`` overrides the env var."""

    other = tmp_path / "other"
    assert ledger_dir(base_dir=other) == other


@pytest.mark.unit
def test_is_disabled_responds_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERA_DOGFOOD_DISABLE", "1")
    assert is_disabled() is True
    monkeypatch.delenv("TESSERA_DOGFOOD_DISABLE")
    assert is_disabled() is False


@pytest.mark.unit
def test_append_creates_directory_lazily(base_dir: Path) -> None:
    """The ledger directory is created on first append, not at import."""

    nested = base_dir / "deep" / "nested"
    ledger = Ledger("sync", base_dir=nested)
    assert not nested.exists()
    record = Record.make(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
    )
    ledger.append(record)
    assert ledger.path.exists()
    assert ledger.path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.unit
def test_append_then_iter_round_trip(base_dir: Path) -> None:
    """Records survive append → iter byte-identical."""

    ledger = Ledger("sync")
    init = Record.make(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
    )
    op = Record.make(
        gate="sync",
        kind="sync_op",
        payload={"command": "push", "exit_code": 0, "elapsed_ms": 1234},
    )
    ledger.append(init)
    ledger.append(op)
    records = list(ledger.iter_records())
    assert records == [init, op]


@pytest.mark.unit
def test_append_rejects_record_for_other_gate(base_dir: Path) -> None:
    """Wiring bug — record.gate must match the ledger."""

    ledger = Ledger("sync")
    record = Record.make(
        gate="compiled",
        kind="note",
        payload={"text": "x"},
    )
    with pytest.raises(SchemaError, match="does not match ledger gate"):
        ledger.append(record)


@pytest.mark.unit
def test_iter_records_surfaces_corruption_with_line_number(
    base_dir: Path,
) -> None:
    """A malformed line must raise with the 1-indexed line number."""

    ledger = Ledger("sync")
    good = Record.make(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
    )
    ledger.append(good)
    # Hand-corrupt the second line.
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    with pytest.raises(LedgerCorruptionError) as excinfo:
        list(ledger.iter_records())
    assert excinfo.value.line_number == 2
    assert excinfo.value.path == ledger.path


@pytest.mark.unit
def test_iter_records_skips_blank_lines(base_dir: Path) -> None:
    """A trailing newline or empty line must not raise corruption."""

    ledger = Ledger("sync")
    good = Record.make(
        gate="sync",
        kind="note",
        payload={"text": "hi"},
    )
    ledger.append(good)
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    records = list(ledger.iter_records())
    assert records == [good]


@pytest.mark.unit
def test_state_uninitialized_on_empty_ledger(base_dir: Path) -> None:
    state = Ledger("sync").state()
    assert state.initialized is False
    assert state.completed is False
    assert state.active is False
    assert state.rows == 0


@pytest.mark.unit
def test_state_active_after_init(base_dir: Path) -> None:
    ledger = Ledger("sync")
    ledger.append(
        Record.make(
            gate="sync",
            kind="gate_initialized",
            payload={"operator": "Tom", "start_date": "2026-05-09"},
        )
    )
    state = ledger.state()
    assert state.initialized is True
    assert state.completed is False
    assert state.active is True
    assert state.rows == 1


@pytest.mark.unit
def test_state_completed_after_close(base_dir: Path) -> None:
    ledger = Ledger("sync")
    ledger.append(
        Record.make(
            gate="sync",
            kind="gate_initialized",
            payload={"operator": "Tom", "start_date": "2026-05-09"},
        )
    )
    ledger.append(
        Record.make(
            gate="sync",
            kind="gate_completed",
            payload={"end_date": "2026-06-12"},
        )
    )
    state = ledger.state()
    assert state.completed is True
    assert state.active is False


@pytest.mark.unit
def test_auto_record_no_op_when_no_gate_active(base_dir: Path) -> None:
    """auto_record returns an empty list when nothing is active."""

    recorded = auto_record(kind="audit_verify", payload={"exit_code": 0})
    assert recorded == []


@pytest.mark.unit
def test_auto_record_targets_only_active_gates(base_dir: Path) -> None:
    """Only initialized-and-not-completed gates receive the row."""

    Ledger("sync").append(
        Record.make(
            gate="sync",
            kind="gate_initialized",
            payload={"operator": "Tom", "start_date": "2026-05-09"},
        )
    )
    Ledger("compiled").append(
        Record.make(
            gate="compiled",
            kind="gate_initialized",
            payload={"operator": "Tom", "start_date": "2026-05-09"},
        )
    )
    Ledger("compiled").append(
        Record.make(
            gate="compiled",
            kind="gate_completed",
            payload={"end_date": "2026-05-10"},
        )
    )
    # playbook gate left uninitialized.
    recorded = auto_record(kind="audit_verify", payload={"exit_code": 0})
    assert recorded == ["sync"]


@pytest.mark.unit
def test_auto_record_filters_by_admitted_kind(base_dir: Path) -> None:
    """A kind only admitted by some gates emits to those gates."""

    for gate in ("sync", "compiled", "playbook"):
        Ledger(gate).append(
            Record.make(
                gate=gate,
                kind="gate_initialized",
                payload={"operator": "Tom", "start_date": "2026-05-09"},
            )
        )
    # ``sync_op`` only admitted by the sync gate; auto_record must not
    # silently fail or write to the other ledgers.
    recorded = auto_record(
        kind="sync_op",
        payload={"command": "push", "exit_code": 0, "elapsed_ms": 100},
    )
    assert recorded == ["sync"]


@pytest.mark.unit
def test_auto_record_explicit_gates_filter(base_dir: Path) -> None:
    """Explicit ``gates=`` argument restricts the dispatch set."""

    for gate in ("sync", "compiled"):
        Ledger(gate).append(
            Record.make(
                gate=gate,
                kind="gate_initialized",
                payload={"operator": "Tom", "start_date": "2026-05-09"},
            )
        )
    recorded = auto_record(
        kind="audit_verify",
        payload={"exit_code": 0},
        gates=["sync"],
    )
    assert recorded == ["sync"]


@pytest.mark.unit
def test_auto_record_skips_when_disabled(base_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``TESSERA_DOGFOOD_DISABLE=1`` short-circuits auto_record."""

    Ledger("sync").append(
        Record.make(
            gate="sync",
            kind="gate_initialized",
            payload={"operator": "Tom", "start_date": "2026-05-09"},
        )
    )
    monkeypatch.setenv("TESSERA_DOGFOOD_DISABLE", "1")
    recorded = auto_record(kind="audit_verify", payload={"exit_code": 0})
    assert recorded == []
    # And no row was actually written to the active ledger.
    rows = list(Ledger("sync").iter_records())
    assert len(rows) == 1  # only the gate_initialized row from setup
    assert rows[0].kind == "gate_initialized"


@pytest.mark.unit
def test_auto_record_raises_on_corrupt_active_ledger(
    base_dir: Path,
) -> None:
    """When an active ledger is corrupt, auto_record surfaces the failure."""

    sync = Ledger("sync")
    sync.append(
        Record.make(
            gate="sync",
            kind="gate_initialized",
            payload={"operator": "Tom", "start_date": "2026-05-09"},
        )
    )
    # Hand-corrupt the file so state() raises during dispatch.
    with sync.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    with pytest.raises(DogfoodEmissionError):
        auto_record(kind="audit_verify", payload={"exit_code": 0})


@pytest.mark.unit
def test_latest_filters_by_kind(base_dir: Path) -> None:
    ledger = Ledger("sync")
    init = Record.make(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
    )
    note = Record.make(gate="sync", kind="note", payload={"text": "hi"})
    audit = Record.make(gate="sync", kind="audit_verify", payload={"exit_code": 0})
    for record in (init, note, audit):
        ledger.append(record)
    assert ledger.latest(kind="audit_verify") == audit
    assert ledger.latest(kind="note") == note
    assert ledger.latest() == audit
