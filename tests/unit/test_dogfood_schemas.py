"""Unit tests for the dogfood evidence record schemas.

Round-trip across JSON, gate-by-kind discriminator validation,
required-key enforcement, frame-field invariants, and the timestamp
shape contract.
"""

from __future__ import annotations

import json
import re

import pytest

from tessera import __version__ as TESSERA_VERSION
from tessera.dogfood.schemas import (
    GATE_KINDS,
    GATES,
    PLAYBOOK_FAILURE_CLASSES,
    SCHEMA_VERSION,
    Record,
    SchemaError,
    machine_id,
    require_kind,
    utc_now_iso,
    validate_payload,
)


@pytest.mark.unit
def test_utc_now_iso_matches_canonical_shape() -> None:
    """``utc_now_iso`` must match the audit chain timestamp pattern."""

    ts = utc_now_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", ts), ts


@pytest.mark.unit
def test_machine_id_is_non_empty_and_includes_arch() -> None:
    """machine_id should carry both hostname and architecture."""

    mid = machine_id()
    assert "|" in mid
    assert len(mid) >= 3


@pytest.mark.unit
def test_gates_constant_is_frozen() -> None:
    """The three gates are sync / compiled / playbook; nothing else."""

    assert frozenset({"sync", "compiled", "playbook"}) == GATES


@pytest.mark.unit
def test_every_gate_admits_the_common_kinds() -> None:
    """Every gate admits the four lifecycle/utility kinds."""

    common = {"gate_initialized", "audit_verify", "note", "gate_completed"}
    for gate in GATES:
        assert common.issubset(GATE_KINDS[gate]), gate


@pytest.mark.unit
def test_sync_op_only_in_sync_gate() -> None:
    """``sync_op`` is sync-specific; not admitted by the other gates."""

    assert "sync_op" in GATE_KINDS["sync"]
    assert "sync_op" not in GATE_KINDS["compiled"]
    assert "sync_op" not in GATE_KINDS["playbook"]


@pytest.mark.unit
def test_recompile_only_in_playbook_gate() -> None:
    """``recompile`` is playbook-specific."""

    assert "recompile" in GATE_KINDS["playbook"]
    assert "recompile" not in GATE_KINDS["compiled"]
    assert "recompile" not in GATE_KINDS["sync"]


@pytest.mark.unit
def test_require_kind_rejects_unknown_gate() -> None:
    with pytest.raises(SchemaError, match="unknown gate"):
        require_kind("unknown", "note")


@pytest.mark.unit
def test_require_kind_rejects_kind_not_in_allowlist() -> None:
    with pytest.raises(SchemaError, match="not admitted by gate"):
        require_kind("sync", "recompile")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("gate", "kind", "payload"),
    [
        ("sync", "gate_initialized", {"operator": "Tom", "start_date": "2026-05-09"}),
        ("sync", "sync_op", {"command": "push", "exit_code": 0, "elapsed_ms": 1234}),
        ("sync", "audit_verify", {"exit_code": 0}),
        ("sync", "note", {"text": "anything"}),
        ("compiled", "register", {"external_id": "ULID", "compiler_version": "v1"}),
        ("compiled", "review", {"usefulness": "high"}),
        ("playbook", "compile", {"target": "t", "compiler_version": "v1", "elapsed_ms": 1}),
        (
            "playbook",
            "failure_case",
            {
                "failure_class": "stale_artifact_trusted_accidentally",
                "target": "t",
                "corrective_action": "expand brief",
            },
        ),
    ],
)
def test_validate_payload_accepts_well_formed_rows(
    gate: str, kind: str, payload: dict[str, object]
) -> None:
    """Well-formed payloads pass validation for their (gate, kind)."""

    validate_payload(gate, kind, payload)


@pytest.mark.unit
def test_validate_payload_rejects_missing_required_key() -> None:
    with pytest.raises(SchemaError, match="missing required keys"):
        validate_payload("sync", "sync_op", {"command": "push"})  # missing exit_code, elapsed_ms


@pytest.mark.unit
def test_validate_payload_tolerates_unknown_keys() -> None:
    """Unknown keys are forward-compatible additions, not errors."""

    validate_payload(
        "sync",
        "sync_op",
        {"command": "push", "exit_code": 0, "elapsed_ms": 12, "novel_key": True},
    )


@pytest.mark.unit
def test_record_make_populates_frame_fields() -> None:
    """``Record.make`` fills schema_version / ts / machine_id / version."""

    record = Record.make(
        gate="sync",
        kind="note",
        payload={"text": "x"},
    )
    assert record.schema_version == SCHEMA_VERSION
    assert record.gate == "sync"
    assert record.kind == "note"
    assert record.tessera_version == TESSERA_VERSION
    assert record.machine_id  # non-empty
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", record.ts)


@pytest.mark.unit
def test_record_make_validates_payload() -> None:
    with pytest.raises(SchemaError, match="missing required keys"):
        Record.make(gate="sync", kind="audit_verify", payload={})


@pytest.mark.unit
def test_record_jsonl_round_trip_byte_identical_with_sorted_keys() -> None:
    """to_jsonl_line / from_jsonl_line round-trips losslessly."""

    record = Record.make(
        gate="playbook",
        kind="register",
        payload={
            "target": "tessera_release_playbook",
            "external_id": "01H...",
            "compiler_version": "claude-code/release-recipe@2026-05-09",
        },
    )
    line = record.to_jsonl_line()
    parsed = Record.from_jsonl_line(line)
    assert parsed == record
    # Stable key ordering — re-serializing must produce the same bytes.
    assert parsed.to_jsonl_line() == line


@pytest.mark.unit
def test_record_from_jsonl_line_rejects_invalid_json() -> None:
    with pytest.raises(SchemaError, match="invalid JSON"):
        Record.from_jsonl_line("{not json")


@pytest.mark.unit
def test_record_from_jsonl_line_rejects_non_object_root() -> None:
    with pytest.raises(SchemaError, match="expected JSON object"):
        Record.from_jsonl_line(json.dumps([1, 2, 3]))


@pytest.mark.unit
def test_record_from_jsonl_line_rejects_missing_frame_field() -> None:
    """A row missing a frame field surfaces the missing key."""

    payload = {
        "schema_version": SCHEMA_VERSION,
        "gate": "sync",
        "kind": "note",
        # ts intentionally absent
        "machine_id": "host|x",
        "tessera_version": "0.5.0rc1",
        "payload": {"text": "x"},
    }
    with pytest.raises(SchemaError, match="missing frame field"):
        Record.from_jsonl_line(json.dumps(payload))


@pytest.mark.unit
def test_record_validate_rejects_drifted_schema_version() -> None:
    """An old reader must refuse a row stamped with a newer schema_version."""

    with pytest.raises(SchemaError, match="unsupported schema_version"):
        Record(
            schema_version=SCHEMA_VERSION + 1,
            gate="sync",
            kind="note",
            ts=utc_now_iso(),
            machine_id="host|x",
            tessera_version="0.5.0rc1",
            payload={"text": "x"},
        ).validate()


@pytest.mark.unit
def test_record_validate_rejects_unknown_gate() -> None:
    with pytest.raises(SchemaError, match="unknown gate"):
        Record(
            schema_version=SCHEMA_VERSION,
            gate="bogus",
            kind="note",
            ts=utc_now_iso(),
            machine_id="host|x",
            tessera_version="0.5.0rc1",
            payload={"text": "x"},
        ).validate()


@pytest.mark.unit
def test_record_validate_rejects_malformed_ts() -> None:
    with pytest.raises(SchemaError, match="ts"):
        Record(
            schema_version=SCHEMA_VERSION,
            gate="sync",
            kind="note",
            ts="2026/05/09 12:34:56",  # wrong separators
            machine_id="host|x",
            tessera_version="0.5.0rc1",
            payload={"text": "x"},
        ).validate()


@pytest.mark.unit
def test_playbook_failure_classes_include_other_sentinel() -> None:
    """The ``other`` sentinel is part of the allowlist."""

    assert "other" in PLAYBOOK_FAILURE_CLASSES
    assert "stale_artifact_trusted_accidentally" in PLAYBOOK_FAILURE_CLASSES
    assert "source_missing_from_compiled_output" in PLAYBOOK_FAILURE_CLASSES
    assert "eval_passed_but_answer_was_weak" in PLAYBOOK_FAILURE_CLASSES
    assert "artifact_too_lossy_for_exploratory_use" in PLAYBOOK_FAILURE_CLASSES
