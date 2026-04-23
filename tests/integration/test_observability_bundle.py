"""End-to-end diagnostic-bundle exercise against a real vault."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from tessera.observability.bundle import BundleSpec, build_bundle, review_instructions
from tessera.observability.events import EventLog
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection


def _seed_vault(open_vault: VaultConnection) -> int:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01BUNDLE', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    vault_capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="bundle test project note",
        source_tool="test",
        captured_at=1_700_000_000,
    )
    return agent_id


@pytest.mark.integration
def test_bundle_round_trip(open_vault: VaultConnection, tmp_path: Path) -> None:
    _seed_vault(open_vault)
    events_path = tmp_path / "events.db"
    log = EventLog.open(events_path)
    try:
        log.emit(level="info", category="embed", event="embed_succeeded", attrs={"facet_id": 1})
        log.emit(
            level="warn",
            category="retrieval",
            event="recall_slow",
            duration_ms=2000,
            attrs={"k": 5, "retrieval_mode": "swcr"},
        )
    finally:
        log.close()
    log = EventLog.open(events_path)
    try:
        spec = BundleSpec(
            vault_conn=open_vault.connection,
            vault_path=tmp_path / "vault.db",
            event_log=log,
            tessera_version="0.0.1.dev0",
            active_models=("ollama/nomic-embed-text",),
        )
        result = build_bundle(spec, out_dir=tmp_path / "bundles", name="smoke")
    finally:
        log.close()
    assert result.tarball_path.exists()
    assert result.tarball_path.name.endswith(".tar.gz")
    with tarfile.open(result.tarball_path, "r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
        assert names == sorted(
            [
                "env.json",
                "config.json",
                "schema.sql",
                "stats.json",
                "recent_events.jsonl",
                "retrieval_samples.jsonl",
                "audit_summary.jsonl",
            ]
        )
        env_member = tar.extractfile("env.json")
        assert env_member is not None
        env = json.loads(env_member.read())
        assert env["tessera_version"] == "0.0.1.dev0"
        events_member = tar.extractfile("recent_events.jsonl")
        assert events_member is not None
        lines = [json.loads(line) for line in events_member.read().decode().splitlines() if line]
        events = {row["event"] for row in lines}
        assert "embed_succeeded" in events
        samples_member = tar.extractfile("retrieval_samples.jsonl")
        assert samples_member is not None
        sample_lines = [
            json.loads(line) for line in samples_member.read().decode().splitlines() if line
        ]
        assert sample_lines
        assert sample_lines[0]["attrs"]["retrieval_mode"] == "swcr"


@pytest.mark.integration
def test_review_instructions_mention_every_bundle_file(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    _seed_vault(open_vault)
    spec = BundleSpec(
        vault_conn=open_vault.connection,
        vault_path=tmp_path / "v.db",
        event_log=None,
        tessera_version="0.0.1.dev0",
        active_models=(),
    )
    result = build_bundle(spec, out_dir=tmp_path / "b", name="review")
    text = review_instructions(result)
    for f in result.files:
        assert f in text
    assert "does not upload" in text.lower() or "only you decide" in text.lower()
