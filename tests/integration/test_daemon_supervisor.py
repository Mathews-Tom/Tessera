"""Daemon start→serve→stop lifecycle with fake adapters.

Patches :func:`tessera.daemon.supervisor.SentenceTransformersReranker`
and :func:`resolve_embedder` so the test exercises the full supervisor
path — vault unlock, HTTP bind, control bind, embed worker loop,
shutdown — without loading the real sentence-transformers model or
reaching Ollama.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.daemon import supervisor
from tessera.daemon.config import resolve_config
from tessera.daemon.control import call_control
from tessera.migration import bootstrap
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt


@pytest.fixture
def short_run_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tess_", dir="/tmp") as tmp:
        yield Path(tmp)


@dataclass
class _FakeEmbedder:
    name: ClassVar[str] = "ollama"
    model_name: str = "ollama"
    dim: int = 8

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [hashlib.sha256(t.encode()).digest()[i] / 255.0 for i in range(self.dim)] for t in texts
        ]

    async def health_check(self) -> None:
        return None


@dataclass
class _FakeReranker:
    name: ClassVar[str] = "fake"
    model_name: str = "length"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def score(
        self, query: str, passages: Sequence[str], *, seed: int | None = None
    ) -> list[float]:
        del query, seed
        return [1.0 / (1 + len(p)) for p in passages]

    async def health_check(self) -> None:
        return None


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supervisor_starts_serves_and_stops(
    short_run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = short_run_dir / "vault.db"
    passphrase = b"lifecycle-test"
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(passphrase), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            cur = vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES ('01SUP', 'a', 0)"
            )
            agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
            models_registry.register_embedding_model(
                vc.connection, name="ollama", dim=8, activate=True
            )
            vault_capture.capture(
                vc.connection,
                agent_id=agent_id,
                facet_type="style",
                content="voice",
                source_tool="t",
                captured_at=1_000_000,
            )
            # Issue token at the real wall clock so it is not already
            # expired by the time the supervisor calls verify_and_touch.
            issued = tokens.issue(
                vc.connection,
                agent_id=agent_id,
                client_name="cli",
                token_class="session",
                scope=build_scope(read=["style"], write=[]),
                now_epoch=int(datetime.now(UTC).timestamp()),
            )
    raw_token = issued.raw_token

    # Swap heavy adapters for lightweight fakes.
    def _fake_resolve(conn: Any, *, ollama_host: str) -> tuple[_FakeEmbedder, int, str]:
        del ollama_host
        model = models_registry.active_model(conn)
        return _FakeEmbedder(), model.id, models_registry.vec_table_name(model.id)

    # supervisor.py binds `resolve_embedder` at import time via
    # `from tessera.daemon.state import resolve_embedder`, so the patch
    # must target the supervisor module's namespace, not daemon_state's.
    monkeypatch.setattr(supervisor, "resolve_embedder", _fake_resolve)
    monkeypatch.setattr(supervisor, "SentenceTransformersReranker", _FakeReranker)

    port = _pick_port()
    config = resolve_config(
        vault_path=vault_path,
        http_port=port,
        socket_path=short_run_dir / "t.sock",
        passphrase=passphrase,
    )
    # Override pid/log/events paths so they live in the short tmp dir.
    config = config.__class__(
        vault_path=config.vault_path,
        http_host=config.http_host,
        http_port=config.http_port,
        socket_path=config.socket_path,
        log_path=short_run_dir / "log",
        pid_path=short_run_dir / "pid",
        events_db_path=short_run_dir / "events.db",
        allowed_origins=config.allowed_origins,
        ollama_host=config.ollama_host,
        reranker_model=config.reranker_model,
        passphrase=config.passphrase,
    )
    stop = asyncio.Event()
    ready = asyncio.Event()
    daemon_task = asyncio.create_task(supervisor.run_daemon(config, stop_event=stop, ready=ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10.0)
        # Control-plane status round-trip.
        status = await call_control(config.socket_path, method="status")
        assert status["vault_id"]
        assert status["active_model_id"]
        # HTTP MCP stats round-trip.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=10.0)
    # Shutdown cleans up socket + pid.
    assert not config.socket_path.exists()
    assert not config.pid_path.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supervisor_emits_daemon_warmed_audit_row(
    short_run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-warmed fake adapters must still land a daemon_warmed audit row
    # so operators can see which reranker tier the daemon is running on.
    # Fake reranker has no resolved_device attribute; the supervisor
    # records "unknown" in that case.
    vault_path = short_run_dir / "vault.db"
    passphrase = b"warmup-test"
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(passphrase), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES ('01WARM', 'a', 0)"
            )
            models_registry.register_embedding_model(
                vc.connection, name="ollama", dim=8, activate=True
            )

    embed_calls: list[list[str]] = []
    score_calls: list[tuple[str, list[str]]] = []

    class _RecordingReranker(_FakeReranker):
        async def score(
            self, query: str, passages: Sequence[str], *, seed: int | None = None
        ) -> list[float]:
            score_calls.append((query, list(passages)))
            return await super().score(query, passages, seed=seed)

    @dataclass
    class _RecordingEmbedder(_FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            embed_calls.append(list(texts))
            return await super().embed(texts)

    def _fake_resolve(conn: Any, *, ollama_host: str) -> tuple[_RecordingEmbedder, int, str]:
        del ollama_host
        model = models_registry.active_model(conn)
        return _RecordingEmbedder(), model.id, models_registry.vec_table_name(model.id)

    # supervisor.py binds `resolve_embedder` at import time via
    # `from tessera.daemon.state import resolve_embedder`, so the patch
    # must target the supervisor module's namespace, not daemon_state's.
    monkeypatch.setattr(supervisor, "resolve_embedder", _fake_resolve)
    monkeypatch.setattr(supervisor, "SentenceTransformersReranker", _RecordingReranker)

    port = _pick_port()
    config = resolve_config(
        vault_path=vault_path,
        http_port=port,
        socket_path=short_run_dir / "w.sock",
        passphrase=passphrase,
    )
    config = config.__class__(
        vault_path=config.vault_path,
        http_host=config.http_host,
        http_port=config.http_port,
        socket_path=config.socket_path,
        log_path=short_run_dir / "log",
        pid_path=short_run_dir / "pid",
        events_db_path=short_run_dir / "events.db",
        allowed_origins=config.allowed_origins,
        ollama_host=config.ollama_host,
        reranker_model=config.reranker_model,
        passphrase=config.passphrase,
    )
    stop = asyncio.Event()
    ready = asyncio.Event()
    daemon_task = asyncio.create_task(
        supervisor.run_daemon(config, stop_event=stop, ready=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10.0)
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=10.0)

    # Both adapters were exercised during warm-up.
    assert embed_calls
    assert embed_calls[0] == ["warm"]
    assert score_calls
    assert score_calls[0][0] == "warm"
    assert len(score_calls[0][1]) >= 2

    # Audit row lands with the expected payload shape.
    import json as _json

    with (
        derive_key(bytearray(passphrase), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        row = vc.connection.execute(
            "SELECT payload FROM audit_log WHERE op='daemon_warmed' ORDER BY at DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "supervisor did not emit a daemon_warmed audit row"
    payload = _json.loads(row[0])
    assert payload["reranker_device"] == "unknown"
    assert payload["embedder_name"] == "_RecordingEmbedder"
    assert isinstance(payload["duration_ms"], int | float)
    assert payload["duration_ms"] >= 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supervisor_refuses_start_without_passphrase(
    short_run_dir: Path,
) -> None:
    config = resolve_config(
        vault_path=short_run_dir / "v.db",
        http_port=_pick_port(),
        socket_path=short_run_dir / "t.sock",
    )
    with pytest.raises(RuntimeError, match="passphrase"):
        await supervisor.run_daemon(config)
