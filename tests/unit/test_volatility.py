"""ADR-0016 memory volatility — capture, SWCR freshness, auto-compaction.

Covers the V0.5-P1 surface end-to-end at the unit level:

* Capture honours the ``volatility``/``ttl_seconds`` parameters and
  rejects illegal combinations.
* SWCR ``freshness(f)`` returns the documented closed-form values
  across persistent / session / ephemeral lifecycles.
* The auto-compaction sweep soft-deletes expired non-persistent rows
  and emits matching audit events; persistent rows are untouched.

Migration coverage lives in ``test_migration_runner.py`` /
``test_vault_schema.py``; the API contract here is what the v0.5
surface guarantees on top of that schema.
"""

from __future__ import annotations

import pathlib

import pytest
import sqlcipher3

from tessera.retrieval import swcr
from tessera.vault import capture, compaction, facets
from tessera.vault.connection import VaultConnection


def _seed_agent(conn: sqlcipher3.Connection) -> int:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'capture-tool', 1)"
    )
    row = conn.execute("SELECT id FROM agents WHERE external_id='a1'").fetchone()
    return int(row[0])


# ---- capture path -------------------------------------------------------


@pytest.mark.unit
def test_capture_default_volatility_is_persistent(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    result = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="active sprint context",
        source_tool="cli",
    )
    row = open_vault.connection.execute(
        "SELECT volatility, ttl_seconds FROM facets WHERE external_id = ?",
        (result.external_id,),
    ).fetchone()
    assert row[0] == "persistent"
    assert row[1] is None
    assert result.volatility == "persistent"
    assert result.ttl_seconds is None


@pytest.mark.unit
def test_capture_session_uses_default_ttl(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    result = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="working memory note",
        source_tool="cli",
        volatility="session",
    )
    assert result.volatility == "session"
    assert result.ttl_seconds == facets.DEFAULT_TTL_SECONDS["session"]


@pytest.mark.unit
def test_capture_ephemeral_with_override_ttl(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    result = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="ephemeral observation",
        source_tool="cli",
        volatility="ephemeral",
        ttl_seconds=600,
    )
    assert result.volatility == "ephemeral"
    assert result.ttl_seconds == 600


@pytest.mark.unit
def test_capture_persistent_rejects_ttl(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    with pytest.raises(facets.InvalidTTLError):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="project",
            content="garbage",
            source_tool="cli",
            volatility="persistent",
            ttl_seconds=300,
        )


@pytest.mark.unit
def test_capture_unknown_volatility_raises(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    with pytest.raises(facets.UnsupportedVolatilityError):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="project",
            content="garbage",
            source_tool="cli",
            volatility="working",
        )


@pytest.mark.unit
def test_capture_ephemeral_ttl_above_ceiling_raises(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    ceiling = facets.MAX_TTL_SECONDS["ephemeral"]
    assert ceiling is not None
    with pytest.raises(facets.InvalidTTLError):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="project",
            content="too long",
            source_tool="cli",
            volatility="ephemeral",
            ttl_seconds=ceiling + 1,
        )


# ---- SWCR freshness term ------------------------------------------------


@pytest.mark.unit
def test_freshness_persistent_is_one() -> None:
    assert swcr.freshness(volatility="persistent", captured_at=0, now=1_000_000) == 1.0


@pytest.mark.unit
def test_freshness_session_linear_decay() -> None:
    captured = 1_000
    ttl = 100
    # Halfway through TTL → 0.5
    assert swcr.freshness(
        volatility="session", captured_at=captured, now=captured + ttl // 2, ttl_seconds=ttl
    ) == pytest.approx(0.5, abs=1e-9)
    # At capture time → 1.0
    assert swcr.freshness(
        volatility="session", captured_at=captured, now=captured, ttl_seconds=ttl
    ) == pytest.approx(1.0)
    # Past TTL → 0.0
    assert (
        swcr.freshness(
            volatility="session", captured_at=captured, now=captured + ttl + 1, ttl_seconds=ttl
        )
        == 0.0
    )


@pytest.mark.unit
def test_freshness_ephemeral_step_decay() -> None:
    captured = 1_000
    ttl = 100
    # Inside the window → 1.0
    assert (
        swcr.freshness(
            volatility="ephemeral", captured_at=captured, now=captured + ttl - 1, ttl_seconds=ttl
        )
        == 1.0
    )
    # At the boundary → 0.0
    assert (
        swcr.freshness(
            volatility="ephemeral", captured_at=captured, now=captured + ttl, ttl_seconds=ttl
        )
        == 0.0
    )


@pytest.mark.unit
def test_freshness_session_falls_back_to_default_ttl() -> None:
    captured = 1_000
    default_ttl = 24 * 3600
    assert swcr.freshness(
        volatility="session", captured_at=captured, now=captured + default_ttl // 2
    ) == pytest.approx(0.5, abs=1e-9)


@pytest.mark.unit
def test_swcr_apply_with_now_weights_freshness() -> None:
    persistent = swcr.SWCRCandidate(
        facet_id=1,
        rerank_score=1.0,
        embedding=[1.0, 0.0],
        facet_type="project",
        entities=frozenset(),
        volatility="persistent",
        captured_at=0,
    )
    session = swcr.SWCRCandidate(
        facet_id=2,
        rerank_score=1.0,
        embedding=[0.0, 1.0],
        facet_type="project",
        entities=frozenset(),
        volatility="session",
        captured_at=1_000,
        ttl_seconds=100,
    )
    results = swcr.apply([persistent, session], now=1_000 + 50)
    by_id = {r.facet_id: r.score for r in results}
    assert by_id[1] > by_id[2]


@pytest.mark.unit
def test_swcr_apply_without_now_matches_v04_behaviour() -> None:
    """When ``now`` is omitted, freshness=1.0 for every candidate."""

    persistent = swcr.SWCRCandidate(
        facet_id=1,
        rerank_score=1.0,
        embedding=[1.0, 0.0],
        facet_type="project",
        entities=frozenset(),
    )
    session = swcr.SWCRCandidate(
        facet_id=2,
        rerank_score=1.0,
        embedding=[0.0, 1.0],
        facet_type="project",
        entities=frozenset(),
        volatility="session",
        captured_at=1_000,
        ttl_seconds=100,
    )
    no_now = {r.facet_id: r.score for r in swcr.apply([persistent, session])}
    assert no_now[1] == pytest.approx(no_now[2])


# ---- auto-compaction sweep ----------------------------------------------


@pytest.mark.unit
def test_compaction_sweep_soft_deletes_expired(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    expired = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="stale session note",
        source_tool="cli",
        volatility="session",
        ttl_seconds=10,
        captured_at=1_000,
    )
    fresh = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="fresh ephemeral",
        source_tool="cli",
        volatility="ephemeral",
        ttl_seconds=60,
        captured_at=10_000,
    )
    persistent = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="durable context",
        source_tool="cli",
    )

    result = compaction.sweep(open_vault.connection, now=1_500)
    assert result.compacted == 1
    assert result.skipped == 0

    is_deleted = {
        row[0]: bool(row[1])
        for row in open_vault.connection.execute(
            "SELECT external_id, is_deleted FROM facets ORDER BY external_id"
        ).fetchall()
    }
    assert is_deleted[expired.external_id] is True
    assert is_deleted[fresh.external_id] is False
    assert is_deleted[persistent.external_id] is False


@pytest.mark.unit
def test_compaction_sweep_emits_audit_event(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    expired = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="ancient session row",
        source_tool="cli",
        volatility="session",
        ttl_seconds=10,
        captured_at=1_000,
    )
    compaction.sweep(open_vault.connection, now=2_000)

    audit_rows = open_vault.connection.execute(
        """
        SELECT op, target_external_id, payload
        FROM audit_log
        WHERE op = 'facet_auto_compacted'
        """
    ).fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0][1] == expired.external_id
    payload = audit_rows[0][2]
    assert "session" in payload
    assert "facet_type" in payload


@pytest.mark.unit
def test_compaction_sweep_no_op_on_persistent_only_vault(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="durable",
        source_tool="cli",
    )
    result = compaction.sweep(open_vault.connection, now=10_000_000)
    assert result.inspected == 0
    assert result.compacted == 0


# ---- migration v3 → v4 -------------------------------------------------


@pytest.mark.unit
def test_v3_to_v4_adds_volatility_column_and_index(open_vault: VaultConnection) -> None:
    cols = {row[1] for row in open_vault.connection.execute("PRAGMA table_info(facets)").fetchall()}
    assert "volatility" in cols
    assert "ttl_seconds" in cols
    indices = {
        row[0]
        for row in open_vault.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "facets_volatility_sweep" in indices


@pytest.mark.unit
def test_v3_to_v4_migration_step_runs_on_v3_vault(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forward migration from a v3 vault leaves rows defaulting to persistent."""

    import tessera.migration.runner as runner_module
    import tessera.vault.connection as connection_module
    import tessera.vault.schema as schema_module
    from tessera.migration.runner import bootstrap, upgrade
    from tessera.vault.connection import BINARY_SCHEMA_VERSION
    from tessera.vault.encryption import derive_key, new_salt

    # Force the bootstrap to land at v3 by temporarily pinning the
    # binary version. The runner's upgrade step list owns the v3→v4
    # transition, so the second pass with the real binary runs it.
    # Routing the patches through ``monkeypatch.setattr`` keeps mypy
    # from objecting to direct assignment against ``Final`` constants
    # while preserving the rollback semantics of the original try /
    # finally.
    salt = new_salt()
    salt_path = tmp_path / "vault.db.salt"
    salt_path.write_bytes(salt)
    vault_path = tmp_path / "vault.db"

    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(connection_module, "BINARY_SCHEMA_VERSION", 3)
    monkeypatch.setattr(runner_module, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(runner_module, "BINARY_SCHEMA_VERSION", 3)
    passphrase = bytearray(b"correct horse battery staple")
    k = derive_key(passphrase, salt)
    bootstrap(vault_path, k)
    k.wipe()
    monkeypatch.undo()

    # Now the binary is back at v4 and upgrade runs the v3→v4 step list.
    passphrase = bytearray(b"correct horse battery staple")
    k2 = derive_key(passphrase, salt)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == BINARY_SCHEMA_VERSION
