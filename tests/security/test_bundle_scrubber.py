"""Adversarial leak-vector tests for ``tessera doctor --collect``.

Each test crafts a plausible way content could slip into a bundle
file, runs the real collector, and asserts the scrubber aborts with
a non-empty violation list. The bundle must not exist on disk when
the scrubber refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.observability.bundle import BundleSpec, build_bundle
from tessera.observability.events import EventLog
from tessera.observability.scrub import ScrubberViolationError
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection


def _seed_agent(open_vault: VaultConnection) -> int:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01LEAK', 'a', 0)"
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


@pytest.mark.security
def test_rogue_event_with_token_is_rejected(open_vault: VaultConnection, tmp_path: Path) -> None:
    agent_id = _seed_agent(open_vault)
    vault_capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="benign",
        source_tool="test",
        captured_at=1,
    )
    events_path = tmp_path / "events.db"
    log = EventLog.open(events_path)
    # Emit a malicious event carrying a Tessera session token — the
    # kind of leak the audit-allowlist discipline is supposed to
    # prevent but the scrubber catches as defence-in-depth.
    # Runtime concatenation so gitleaks' scan of this source file does
    # not flag the fixture as a real leak.
    rogue_token = "tessera_session_" + "A" * 24
    log.emit(
        level="info",
        category="auth",
        event="token_issued",
        attrs={"secret_token": rogue_token},
    )
    log.close()

    log = EventLog.open(events_path)
    try:
        spec = BundleSpec(
            vault_conn=open_vault.connection,
            vault_path=tmp_path / "v.db",
            event_log=log,
            tessera_version="0.0.1.dev0",
            active_models=(),
        )
        with pytest.raises(ScrubberViolationError):
            build_bundle(spec, out_dir=tmp_path / "b", name="leak")
    finally:
        log.close()
    # Tarball must not have been produced.
    assert not list((tmp_path / "b").glob("*.tar.gz"))


@pytest.mark.security
def test_rogue_event_with_long_string_is_rejected(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _seed_agent(open_vault)
    vault_capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="benign",
        source_tool="test",
        captured_at=1,
    )
    events_path = tmp_path / "events.db"
    log = EventLog.open(events_path)
    # A 1 KB blob in attrs is below the events.db per-row cap but well
    # over the scrubber's string-length cap — exactly the leak vector
    # the cap is calibrated to catch.
    log.emit(
        level="info",
        category="retrieval",
        event="recall_slow",
        attrs={"notes": "x" * 1024},
    )
    log.close()

    log = EventLog.open(events_path)
    try:
        spec = BundleSpec(
            vault_conn=open_vault.connection,
            vault_path=tmp_path / "v.db",
            event_log=log,
            tessera_version="0.0.1.dev0",
            active_models=(),
        )
        with pytest.raises(ScrubberViolationError, match="string_length_cap"):
            build_bundle(spec, out_dir=tmp_path / "b", name="leak")
    finally:
        log.close()
    assert not list((tmp_path / "b").glob("*.tar.gz"))


@pytest.mark.security
def test_rogue_event_with_openai_key_is_rejected(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _seed_agent(open_vault)
    vault_capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="benign",
        source_tool="test",
        captured_at=1,
    )
    events_path = tmp_path / "events.db"
    log = EventLog.open(events_path)
    leaked_key = "sk-" + "0123456789abcdefghijABCDEFGHIJKL"
    log.emit(
        level="error",
        category="embed",
        event="embed_failed",
        attrs={"note": f"called with {leaked_key}"},
    )
    log.close()

    log = EventLog.open(events_path)
    try:
        spec = BundleSpec(
            vault_conn=open_vault.connection,
            vault_path=tmp_path / "v.db",
            event_log=log,
            tessera_version="0.0.1.dev0",
            active_models=(),
        )
        with pytest.raises(ScrubberViolationError, match="openai_api_key"):
            build_bundle(spec, out_dir=tmp_path / "b", name="leak")
    finally:
        log.close()
    assert not list((tmp_path / "b").glob("*.tar.gz"))
