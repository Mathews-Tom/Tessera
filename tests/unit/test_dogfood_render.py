"""Unit tests for the dogfood Acceptance Summary predicates.

The Evidence Log is exercised end-to-end via integration tests; these
tests pin the predicate logic (the per-gate Met/Pending decisions)
directly against synthetic record lists so a regression that loosens
or breaks a predicate surfaces before the integration layer.

The fixtures synthesize records via ``Record.make`` so the timestamp
and machine_id come from real helpers — predicates that read those
frame fields stay honest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tessera.dogfood.render import (
    render_acceptance_summary,
    render_evidence_log,
)
from tessera.dogfood.schemas import Record


def _ts(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record(
    *,
    gate: str,
    kind: str,
    payload: dict[str, object],
    when: datetime | None = None,
    host: str | None = None,
) -> Record:
    return Record.make(
        gate=gate,
        kind=kind,
        payload=payload,
        ts=_ts(when) if when is not None else None,
        host=host,
    )


# ---- compiled gate: registered_through_path filters by exit_code -------


@pytest.mark.unit
def test_compiled_registered_through_path_requires_successful_register() -> None:
    """A failed register row does not satisfy the storage predicate."""

    failed = _record(
        gate="compiled",
        kind="register",
        payload={
            "external_id": "",
            "compiler_version": "v1",
            "exit_code": 1,
            "error_class": "InvalidCompiledArtifactError",
        },
    )
    table = render_acceptance_summary([failed], gate="compiled")
    assert "Compiled artifact registered through shipped path | Pending" in table


@pytest.mark.unit
def test_compiled_registered_through_path_clears_on_successful_register() -> None:
    success = _record(
        gate="compiled",
        kind="register",
        payload={
            "external_id": "01H...",
            "compiler_version": "v1",
            "exit_code": 0,
        },
    )
    table = render_acceptance_summary([success], gate="compiled")
    assert "Compiled artifact registered through shipped path | Met" in table
    assert "1 successful register row(s)" in table


@pytest.mark.unit
def test_compiled_registered_through_path_does_not_count_failed_among_successes() -> None:
    """A failed and a successful register together still count as one success."""

    failed = _record(
        gate="compiled",
        kind="register",
        payload={
            "external_id": "",
            "compiler_version": "v1",
            "exit_code": 1,
        },
    )
    success = _record(
        gate="compiled",
        kind="register",
        payload={
            "external_id": "01H...",
            "compiler_version": "v1",
            "exit_code": 0,
        },
    )
    table = render_acceptance_summary([failed, success], gate="compiled")
    assert "1 successful register row(s)" in table
    assert "Compiled artifact registered through shipped path | Met" in table


# ---- playbook gate: register filters + register-as-cli evidence --------


@pytest.mark.unit
def test_playbook_two_targets_registered_filters_failed_registers() -> None:
    """Two FAILED registers against distinct targets do not clear the predicate."""

    f1 = _record(
        gate="playbook",
        kind="register",
        payload={
            "target": "release_playbook",
            "external_id": "",
            "compiler_version": "v1",
            "exit_code": 1,
        },
    )
    f2 = _record(
        gate="playbook",
        kind="register",
        payload={
            "target": "swcr_design_brief",
            "external_id": "",
            "compiler_version": "v1",
            "exit_code": 1,
        },
    )
    table = render_acceptance_summary([f1, f2], gate="playbook")
    assert "Two or more Phase 9 targets registered | Pending" in table


@pytest.mark.unit
def test_playbook_two_targets_registered_clears_on_two_successes() -> None:
    s1 = _record(
        gate="playbook",
        kind="register",
        payload={
            "target": "release_playbook",
            "external_id": "01H...A",
            "compiler_version": "v1",
            "exit_code": 0,
        },
    )
    s2 = _record(
        gate="playbook",
        kind="register",
        payload={
            "target": "swcr_design_brief",
            "external_id": "01H...B",
            "compiler_version": "v1",
            "exit_code": 0,
        },
    )
    table = render_acceptance_summary([s1, s2], gate="playbook")
    assert "Two or more Phase 9 targets registered | Met" in table
    assert "release_playbook" in table
    assert "swcr_design_brief" in table


@pytest.mark.unit
def test_playbook_register_recompile_predicate_uses_register_not_compile() -> None:
    """The predicate must clear on auto-emitted register + manual recompile.

    The shipped CLI never emits ``kind == "compile"`` automatically (per
    ADR 0019 §Boundary statement); register is what actually evidences a
    compile loop completing. A successful register plus a recompile row
    must clear the predicate.
    """

    success = _record(
        gate="playbook",
        kind="register",
        payload={
            "target": "release_playbook",
            "external_id": "01H...A",
            "compiler_version": "v1",
            "exit_code": 0,
        },
    )
    recompile = _record(
        gate="playbook",
        kind="recompile",
        payload={
            "target": "release_playbook",
            "old_external_id": "01H...A",
            "new_external_id": "01H...B",
            "compiler_version": "v2",
        },
    )
    table = render_acceptance_summary([success, recompile], gate="playbook")
    assert "Register and recompile both driven through the shipped CLI | Met" in table
    assert "register=True, recompile=True" in table


@pytest.mark.unit
def test_playbook_register_recompile_predicate_pending_without_register() -> None:
    """A recompile row alone does not clear the predicate."""

    recompile = _record(
        gate="playbook",
        kind="recompile",
        payload={
            "target": "release_playbook",
            "old_external_id": "01H...A",
            "new_external_id": "01H...B",
            "compiler_version": "v2",
        },
    )
    table = render_acceptance_summary([recompile], gate="playbook")
    assert "Register and recompile both driven through the shipped CLI | Pending" in table
    assert "register=False, recompile=True" in table


# ---- sync gate: audit_verified is per-machine --------------------------


@pytest.mark.unit
def test_sync_audit_verified_requires_pass_on_each_machine() -> None:
    """A passing audit_verify on one machine doesn't clear the gate when
    a second machine has only sync_op rows."""

    init = _record(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
        host="laptop-a|arm64",
    )
    op_a = _record(
        gate="sync",
        kind="sync_op",
        payload={"command": "push", "exit_code": 0, "elapsed_ms": 100},
        host="laptop-a|arm64",
    )
    op_b = _record(
        gate="sync",
        kind="sync_op",
        payload={"command": "pull", "exit_code": 0, "elapsed_ms": 110},
        host="laptop-b|arm64",
    )
    audit_a = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 0, "outcome": "intact"},
        host="laptop-a|arm64",
    )
    table = render_acceptance_summary([init, op_a, op_b, audit_a], gate="sync")
    assert "Audit verification passed after pull | Pending" in table
    assert "machines without passing audit_verify: laptop-b|arm64" in table


@pytest.mark.unit
def test_sync_audit_verified_clears_when_each_machine_has_one_pass() -> None:
    init = _record(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
        host="laptop-a|arm64",
    )
    audit_a = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 0, "outcome": "intact"},
        host="laptop-a|arm64",
    )
    audit_b = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 0, "outcome": "intact"},
        host="laptop-b|arm64",
    )
    table = render_acceptance_summary([init, audit_a, audit_b], gate="sync")
    assert "Audit verification passed after pull | Met" in table
    assert "audit_verify exit=0 on every machine (2/2)" in table


@pytest.mark.unit
def test_sync_audit_verified_pending_when_one_machine_only_failed() -> None:
    """A failing audit_verify does not satisfy per-machine coverage."""

    init = _record(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
        host="laptop-a|arm64",
    )
    audit_a_pass = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 0, "outcome": "intact"},
        host="laptop-a|arm64",
    )
    audit_b_fail = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 1, "outcome": "broken_row"},
        host="laptop-b|arm64",
    )
    table = render_acceptance_summary([init, audit_a_pass, audit_b_fail], gate="sync")
    assert "Audit verification passed after pull | Pending" in table
    assert "machines without passing audit_verify: laptop-b|arm64" in table


# ---- audit_verify Notes column: M2 ------------------------------------


@pytest.mark.unit
def test_evidence_log_audit_verify_notes_show_outcome() -> None:
    """The audit_verify row's Notes column surfaces the outcome string."""

    record = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 0, "outcome": "intact", "total_rows": 17},
        host="laptop|arm64",
    )
    table = render_evidence_log([record], gate="sync")
    # The last column on a sync row is "Notes"; outcome must appear there.
    assert "intact" in table
    # The placeholder em-dash from the old behaviour must not appear in
    # the Notes cell when outcome is populated.
    assert "| — |\n" not in table


@pytest.mark.unit
def test_evidence_log_audit_verify_notes_show_exit_when_failed() -> None:
    """A failed audit_verify shows the outcome plus the exit code."""

    record = _record(
        gate="sync",
        kind="audit_verify",
        payload={"exit_code": 1, "outcome": "broken_row", "row_id": 42, "op": "facet_inserted"},
        host="laptop|arm64",
    )
    table = render_evidence_log([record], gate="sync")
    assert "broken_row (exit=1)" in table


# ---- predicate days helper across timezones-equivalent runs ----------


@pytest.mark.unit
def test_sync_days_predicate_clears_after_30_days() -> None:
    start = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    init = _record(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
        when=start,
        host="laptop|arm64",
    )
    later = _record(
        gate="sync",
        kind="note",
        payload={"text": "30 days in"},
        when=start + timedelta(days=30, hours=1),
        host="laptop|arm64",
    )
    table = render_acceptance_summary([init, later], gate="sync")
    assert "30 consecutive days completed | Met" in table
    assert "30 days from 2026-05-09 to 2026-06-08" in table


@pytest.mark.unit
def test_sync_days_predicate_pending_below_threshold() -> None:
    start = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    init = _record(
        gate="sync",
        kind="gate_initialized",
        payload={"operator": "Tom", "start_date": "2026-05-09"},
        when=start,
        host="laptop|arm64",
    )
    later = _record(
        gate="sync",
        kind="note",
        payload={"text": "early"},
        when=start + timedelta(days=10),
        host="laptop|arm64",
    )
    table = render_acceptance_summary([init, later], gate="sync")
    assert "30 consecutive days completed | Pending" in table
