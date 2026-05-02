"""V0.5-P5 automation registry — vault layer per ADR 0020.

Coverage map:

* **validate_metadata** — closed contract enforcement (required +
  permitted keys, ULID agent_ref, ISO-8601 last_run, length bounds).
* **register / record_run** — happy paths plus the cross-agent
  boundary on the update.
* **get / list_for_agent** — soft-delete filtering and the
  ``runner`` filter for caller-side scoping.
* **Audit emission** — ``automation_run_recorded`` lands with the
  bucketed payload (free-form prose stays out of the chain).
* **Storage boundary** — the daemon must not emit any extra audit
  ops for the register path beyond ``facet_inserted`` (registers
  ride the standard capture path).
"""

from __future__ import annotations

import json

import pytest
import sqlcipher3

from tessera.vault import audit, automations
from tessera.vault.connection import VaultConnection


def _seed_agent(conn: sqlcipher3.Connection, external_id: str = "a1") -> int:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 1)",
        (external_id, f"agent-{external_id}"),
    )
    row = conn.execute(
        "SELECT id FROM agents WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    return int(row[0])


def _valid_metadata(
    *,
    agent_ref: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    runner: str = "cron",
) -> dict[str, object]:
    return {
        "agent_ref": agent_ref,
        "trigger_spec": "cron 0 9 * * *",
        "cadence": "daily 09:00",
        "runner": runner,
    }


# ---- validate_metadata ---------------------------------------------------


@pytest.mark.unit
def test_validate_metadata_happy_path() -> None:
    meta = automations.validate_metadata(_valid_metadata())
    assert meta.agent_ref == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert meta.trigger_spec == "cron 0 9 * * *"
    assert meta.cadence == "daily 09:00"
    assert meta.runner == "cron"
    assert meta.last_run is None
    assert meta.last_result is None


@pytest.mark.unit
def test_validate_metadata_accepts_optional_last_run_and_result() -> None:
    meta = automations.validate_metadata(
        {
            **_valid_metadata(),
            "last_run": "2026-05-02T09:00:00Z",
            "last_result": "success",
        }
    )
    assert meta.last_run == "2026-05-02T09:00:00Z"
    assert meta.last_result == "success"


@pytest.mark.unit
def test_validate_metadata_rejects_unknown_key() -> None:
    with pytest.raises(automations.InvalidAutomationMetadataError, match="unknown keys"):
        automations.validate_metadata({**_valid_metadata(), "next_run": "tomorrow"})


@pytest.mark.unit
def test_validate_metadata_rejects_missing_required_key() -> None:
    bad = _valid_metadata()
    del bad["runner"]
    with pytest.raises(automations.InvalidAutomationMetadataError, match="missing required"):
        automations.validate_metadata(bad)


@pytest.mark.unit
def test_validate_metadata_rejects_non_ulid_agent_ref() -> None:
    with pytest.raises(automations.InvalidAutomationMetadataError, match="agent_ref"):
        automations.validate_metadata({**_valid_metadata(), "agent_ref": "not-a-ulid"})


@pytest.mark.unit
def test_validate_metadata_rejects_malformed_iso_timestamp() -> None:
    with pytest.raises(automations.InvalidAutomationMetadataError, match="ISO-8601"):
        automations.validate_metadata(
            {**_valid_metadata(), "last_run": "yesterday at noon"},
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "max_chars"),
    [
        ("trigger_spec", 1_024),
        ("cadence", 256),
        ("runner", 128),
    ],
)
def test_validate_metadata_rejects_overlong_required_field(field: str, max_chars: int) -> None:
    """Each required-string field has its length ceiling pinned by a
    parametrised test. A regression that loosens any one cap surfaces
    as a missing parametric case, not a silent acceptance."""

    bad = _valid_metadata()
    bad[field] = "x" * (max_chars + 1)
    with pytest.raises(automations.InvalidAutomationMetadataError, match="length"):
        automations.validate_metadata(bad)


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_last_result() -> None:
    with pytest.raises(automations.InvalidAutomationMetadataError, match="length"):
        automations.validate_metadata(
            {**_valid_metadata(), "last_result": "x" * 1_025},
        )


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_last_run() -> None:
    with pytest.raises(automations.InvalidAutomationMetadataError, match="length"):
        automations.validate_metadata(
            {**_valid_metadata(), "last_run": "x" * 65},
        )


@pytest.mark.unit
def test_validate_metadata_rejects_non_string_runner() -> None:
    with pytest.raises(automations.InvalidAutomationMetadataError, match="must be a string"):
        automations.validate_metadata({**_valid_metadata(), "runner": 42})


# ---- register ------------------------------------------------------------


@pytest.mark.unit
def test_register_inserts_facet_and_returns_external_id(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, is_new = automations.register(
        conn,
        agent_id=agent_id,
        content="Daily standup digest at 09:00.",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    assert is_new is True
    row = conn.execute(
        "SELECT facet_type, content FROM facets WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "automation"
    assert row[1] == "Daily standup digest at 09:00."


@pytest.mark.unit
def test_register_rejects_invalid_metadata_before_insert(
    open_vault: VaultConnection,
) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    bad = _valid_metadata()
    del bad["cadence"]
    with pytest.raises(automations.InvalidAutomationMetadataError):
        automations.register(
            conn,
            agent_id=agent_id,
            content="x",
            metadata=bad,
            source_tool="cli",
        )
    # No row landed.
    row = conn.execute("SELECT COUNT(*) FROM facets WHERE facet_type = 'automation'").fetchone()
    assert int(row[0]) == 0


@pytest.mark.unit
def test_register_writes_facet_inserted_audit_only(open_vault: VaultConnection) -> None:
    """Register rides the standard capture path. The audit chain
    grows by exactly one ``facet_inserted`` row — no second
    automation-specific op fires on the register side."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    before_rows = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    after_rows = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    assert int(after_rows[0]) == int(before_rows[0]) + 1
    last_op = conn.execute("SELECT op FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert last_op[0] == "facet_inserted"


# ---- record_run ----------------------------------------------------------


@pytest.mark.unit
def test_record_run_updates_metadata_and_emits_audit(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="Daily digest.",
        metadata=_valid_metadata(),
        source_tool="cli",
    )

    automations.record_run(
        conn,
        agent_id=agent_id,
        external_id=external_id,
        last_run="2026-05-02T09:00:05Z",
        last_result="success",
    )

    raw = conn.execute(
        "SELECT metadata FROM facets WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    meta = json.loads(raw[0])
    assert meta["last_run"] == "2026-05-02T09:00:05Z"
    assert meta["last_result"] == "success"
    audit_row = conn.execute(
        """
        SELECT op, payload FROM audit_log
        WHERE op = 'automation_run_recorded' AND target_external_id = ?
        """,
        (external_id,),
    ).fetchone()
    payload = json.loads(audit_row[1])
    assert payload == {
        "result_bucket": "success",
        "last_run_at": "2026-05-02T09:00:05Z",
    }


@pytest.mark.unit
def test_record_run_buckets_free_form_result_as_other(
    open_vault: VaultConnection,
) -> None:
    """Free-form ``last_result`` notes never enter the audit
    payload as user content — the chain carries the canonical
    bucket (``other`` for non-bucket values), the row's metadata
    column holds the full string."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    automations.record_run(
        conn,
        agent_id=agent_id,
        external_id=external_id,
        last_run="2026-05-02T10:00:00Z",
        last_result="partial: 3/5 sources scraped, retrying tomorrow",
    )
    payload = json.loads(
        conn.execute(
            "SELECT payload FROM audit_log WHERE op = 'automation_run_recorded' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    )
    assert payload["result_bucket"] == "other"
    raw_meta = json.loads(
        conn.execute(
            "SELECT metadata FROM facets WHERE external_id = ?", (external_id,)
        ).fetchone()[0]
    )
    assert raw_meta["last_result"].startswith("partial: 3/5")


@pytest.mark.unit
def test_record_run_blocks_cross_agent_update(open_vault: VaultConnection) -> None:
    """Agent B cannot record a run on Agent A's automation even with
    a known ULID. The storage layer's ``agent_id`` filter is the
    cross-agent isolation boundary independent of the MCP-layer
    scope check."""

    conn = open_vault.connection
    agent_a = _seed_agent(conn, external_id="a1")
    agent_b = _seed_agent(conn, external_id="b1")
    external_id, _ = automations.register(
        conn,
        agent_id=agent_a,
        content="Agent A's automation.",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    with pytest.raises(automations.UnknownAutomationError, match="for this agent"):
        automations.record_run(
            conn,
            agent_id=agent_b,
            external_id=external_id,
            last_run="2026-05-02T09:00:00Z",
            last_result="success",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bucket_input", "expected_payload"),
    [
        ("success", "success"),
        ("partial", "partial"),
        ("failure", "failure"),
        ("partial: 3/5 sources scraped", "other"),
        ("custom prose", "other"),
    ],
)
def test_record_run_buckets_each_canonical_value(
    open_vault: VaultConnection,
    bucket_input: str,
    expected_payload: str,
) -> None:
    """Every canonical bucket reaches the audit chain unchanged;
    every free-form value lands as ``"other"``. A regression that
    mistypes one bucket (e.g. ``"failed"`` vs ``"failure"``) would
    surface here."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content=f"automation for bucket {bucket_input}",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    automations.record_run(
        conn,
        agent_id=agent_id,
        external_id=external_id,
        last_run="2026-05-02T09:00:00Z",
        last_result=bucket_input,
    )
    payload = json.loads(
        conn.execute(
            "SELECT payload FROM audit_log WHERE op = 'automation_run_recorded' "
            "AND target_external_id = ? ORDER BY id DESC LIMIT 1",
            (external_id,),
        ).fetchone()[0]
    )
    assert payload["result_bucket"] == expected_payload


@pytest.mark.unit
def test_record_run_blocks_soft_deleted_automation(open_vault: VaultConnection) -> None:
    """Soft-deleted automations cannot accept new run records. The
    SQL filter on ``is_deleted = 0`` is the regression guard — a
    refactor that drops the predicate would let runners mutate
    tombstoned rows and emit cascade audit rows for facets the user
    believes are gone."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    conn.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (external_id,),
    )
    with pytest.raises(automations.UnknownAutomationError, match="for this agent"):
        automations.record_run(
            conn,
            agent_id=agent_id,
            external_id=external_id,
            last_run="2026-05-02T09:00:00Z",
            last_result="success",
        )


@pytest.mark.unit
def test_record_run_surfaces_corrupt_metadata_distinctly(
    open_vault: VaultConnection,
) -> None:
    """A vault row whose metadata JSON is malformed must surface as
    ``CorruptAutomationRowError`` (mapped to ``StorageError`` at the
    MCP boundary) rather than as ``InvalidAutomationMetadataError``
    (which is for caller-input drift). Pin the distinction so the
    MCP boundary's exception-mapping cannot regress."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    # Plant a malformed metadata blob directly to simulate a corrupt
    # row reaching ``record_run`` (e.g., a partial restore, a buggy
    # migration, a manual edit).
    conn.execute(
        "UPDATE facets SET metadata = ? WHERE external_id = ?",
        ("not a json blob", external_id),
    )
    with pytest.raises(automations.CorruptAutomationRowError, match="not valid JSON"):
        automations.record_run(
            conn,
            agent_id=agent_id,
            external_id=external_id,
            last_run="2026-05-02T09:00:00Z",
            last_result="success",
        )


@pytest.mark.unit
def test_get_surfaces_corrupt_metadata_distinctly(
    open_vault: VaultConnection,
) -> None:
    """The same corruption surfaces from the read path
    (``_row_to_automation``) so ``get`` and ``list_for_agent`` do
    not throw an opaque ``json.JSONDecodeError`` to the MCP layer."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    conn.execute(
        "UPDATE facets SET metadata = ? WHERE external_id = ?",
        ("not a json blob", external_id),
    )
    with pytest.raises(automations.CorruptAutomationRowError, match="not valid JSON"):
        automations.get(conn, external_id=external_id)


@pytest.mark.unit
def test_record_run_rejects_malformed_timestamp(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    with pytest.raises(automations.InvalidAutomationMetadataError, match="ISO-8601"):
        automations.record_run(
            conn,
            agent_id=agent_id,
            external_id=external_id,
            last_run="9 AM",
            last_result="success",
        )


@pytest.mark.unit
def test_record_run_raises_on_unknown_external_id(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    with pytest.raises(automations.UnknownAutomationError):
        automations.record_run(
            conn,
            agent_id=agent_id,
            external_id="01NEVERREGISTERED4567890",
            last_run="2026-05-02T09:00:00Z",
            last_result="success",
        )


# ---- get / list_for_agent ------------------------------------------------


@pytest.mark.unit
def test_get_returns_full_view(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="Hourly monitor.",
        metadata=_valid_metadata(runner="systemd_timer"),
        source_tool="cli",
    )
    auto = automations.get(conn, external_id=external_id)
    assert auto is not None
    assert auto.metadata.runner == "systemd_timer"
    assert auto.content == "Hourly monitor."


@pytest.mark.unit
def test_get_returns_none_for_soft_deleted(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    external_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="x",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    conn.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (external_id,),
    )
    assert automations.get(conn, external_id=external_id) is None


@pytest.mark.unit
def test_list_for_agent_filters_by_runner(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    cron_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="cron job",
        metadata=_valid_metadata(runner="cron"),
        source_tool="cli",
    )
    schedule_id, _ = automations.register(
        conn,
        agent_id=agent_id,
        content="claude /schedule job",
        metadata=_valid_metadata(runner="claude_code_schedule"),
        source_tool="cli",
    )

    crons = automations.list_for_agent(conn, agent_id=agent_id, runner="cron")
    assert {a.external_id for a in crons} == {cron_id}

    schedules = automations.list_for_agent(conn, agent_id=agent_id, runner="claude_code_schedule")
    assert {a.external_id for a in schedules} == {schedule_id}

    every = automations.list_for_agent(conn, agent_id=agent_id)
    assert {a.external_id for a in every} == {cron_id, schedule_id}


@pytest.mark.unit
def test_list_for_agent_excludes_other_agents(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_a = _seed_agent(conn, external_id="a1")
    agent_b = _seed_agent(conn, external_id="b1")
    a_id, _ = automations.register(
        conn,
        agent_id=agent_a,
        content="A's automation",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    automations.register(
        conn,
        agent_id=agent_b,
        content="B's automation",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    a_list = automations.list_for_agent(conn, agent_id=agent_a)
    assert {a.external_id for a in a_list} == {a_id}


# ---- audit allowlist contract -------------------------------------------


@pytest.mark.unit
def test_audit_allowlist_includes_automation_run_recorded() -> None:
    assert "automation_run_recorded" in audit.allowed_ops()
    assert audit.allowed_keys("automation_run_recorded") == frozenset(
        {"result_bucket", "last_run_at"}
    )
