"""Daemon supervisor: start, run, stop.

Wires the four long-lived pieces — unlocked vault, HTTP MCP server,
Unix control-plane server, embed-worker loop — into one asyncio task
graph. ``run()`` blocks until SIGTERM/SIGINT and then closes each
piece cleanly, in reverse-startup order, so the vault is the last
thing released.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tessera.adapters.fastembed_reranker import FastEmbedReranker
from tessera.adapters.protocol import Embedder, Reranker
from tessera.auth.tokens import VerifiedCapability
from tessera.daemon.config import DaemonConfig
from tessera.daemon.control import ControlError, ControlRequest, serve_control_socket
from tessera.daemon.dispatch import UnknownMethodError, dispatch_tool_call
from tessera.daemon.exchange import NonceStore
from tessera.daemon.http_mcp import serve_http_mcp
from tessera.daemon.state import DaemonState, open_vault_for_daemon, resolve_embedder
from tessera.observability.events import EventLog
from tessera.retrieval import embed_worker
from tessera.vault import audit
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, load_salt

_EMBED_WORKER_BATCH = 128
_EMBED_WORKER_IDLE_SECONDS = 5.0
# Sweep events.db once per hour. The retention is 7 days so the sweep
# is latency-tolerant, but running hourly keeps the file small on
# dogfooding vaults without waking the CPU more than once per 3600
# iterations of the embed loop.
_EVENTS_SWEEP_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class DaemonHandles:
    """Running daemon: what the supervisor started, what the shutdown releases."""

    state: DaemonState
    http_server: asyncio.AbstractServer
    control_server: asyncio.AbstractServer
    embed_task: asyncio.Task[None]
    events_task: asyncio.Task[None]
    event_log: EventLog


async def run_daemon(
    config: DaemonConfig,
    *,
    stop_event: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
) -> None:
    """Launch the daemon, serve, and exit on SIGTERM/SIGINT.

    ``stop_event`` is an optional external kill switch; pass it from a
    test to shut the daemon down without signals. ``ready`` fires once
    the control socket, HTTP server, and vault are all up.
    """

    key = _derive_key(config)
    with key:
        vault = open_vault_for_daemon(config.vault_path, key)
        try:
            embedder, active_model_id, vec_table = resolve_embedder(vault.connection)
            reranker = FastEmbedReranker(model_name=config.reranker_model)
            event_log = EventLog.open(config.events_db_path)
            await _warm_adapters(vault, embedder, reranker)
            state = DaemonState(
                vault_path=config.vault_path,
                vault=vault,
                embedder=embedder,
                reranker=reranker,
                active_model_id=active_model_id,
                vec_table=vec_table,
                vault_id=vault.state.vault_id,
                event_log=event_log,
            )
            stop = stop_event or asyncio.Event()
            _install_signal_handlers(stop)
            nonce_store = NonceStore()
            http_server = await serve_http_mcp(
                host=config.http_host,
                port=config.http_port,
                allowed_origins=config.allowed_origins,
                conn=vault.connection,
                dispatch=_make_tool_dispatcher(state),
                now_epoch_fn=_now_epoch,
                nonce_store=nonce_store,
            )
            control_server = await serve_control_socket(
                socket_path=config.socket_path,
                dispatch=_make_control_dispatcher(state, stop, nonce_store),
            )
            embed_task = asyncio.create_task(
                _embed_worker_loop(state, stop), name="tessera.embed_worker"
            )
            events_task = asyncio.create_task(
                _events_sweep_loop(event_log, stop), name="tessera.events_sweep"
            )
            _write_pid_file(config.pid_path)
            if ready is not None:
                ready.set()
            try:
                await stop.wait()
            finally:
                await _shutdown(
                    DaemonHandles(
                        state=state,
                        http_server=http_server,
                        control_server=control_server,
                        embed_task=embed_task,
                        events_task=events_task,
                        event_log=event_log,
                    ),
                    socket_path=config.socket_path,
                    pid_path=config.pid_path,
                )
        finally:
            vault.close()


def _derive_key(config: DaemonConfig) -> ProtectedKey:
    if config.passphrase is None:
        raise RuntimeError(
            "daemon requires a passphrase at startup; pass it via the CLI "
            "or set TESSERA_PASSPHRASE before launching tesserad"
        )
    salt = load_salt(config.vault_path)
    return derive_key(bytearray(config.passphrase), salt)


async def _warm_adapters(vault: VaultConnection, embedder: Embedder, reranker: Reranker) -> None:
    """Force both adapters to load before the daemon accepts traffic.

    fastembed defers ONNX session creation to the first ``embed`` /
    ``score`` call; the first call after a cold start pays the
    weight-load cost (~2-5 s once weights are cached on disk, ~30 s
    on a first-ever start that downloads ~520 MB of embedder weights
    + ~130 MB of cross-encoder weights). Without this warm-up, the
    first MCP recall after daemon start pays both costs in series.

    The reranker's ``score`` call uses two dummy passages because the
    adapter's own ``health_check`` documents the same workaround
    (single-row cross-encoder forward passes have NaN'd on some older
    onnxruntime builds).

    Fails loud: any exception propagates and blocks daemon startup,
    consistent with the no-fallback policy. A user who sees this error
    has either a broken fastembed install or a permission problem on
    ``~/.cache/fastembed``; a silent fallback to a non-functional
    daemon would hide it.
    """

    start = time.perf_counter()
    await embedder.embed(["warm"])
    await reranker.score("warm", ["warm up", "probe"])
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    resolved_device = getattr(reranker, "resolved_device", "") or "unknown"
    audit.write(
        vault.connection,
        op="daemon_warmed",
        actor="daemon",
        payload={
            "reranker_device": resolved_device,
            "embedder_name": type(embedder).__name__,
            "duration_ms": round(elapsed_ms, 1),
        },
    )


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _events_sweep_loop(event_log: EventLog, stop: asyncio.Event) -> None:
    """Drop events past the retention window on a slow cadence.

    Runs hourly per :data:`_EVENTS_SWEEP_SECONDS`; the retention is
    7 days by default so the sweep is latency-tolerant. A failing
    sweep logs to stderr and retries on the next interval — the
    retention policy is best-effort, not a correctness invariant.
    """

    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_EVENTS_SWEEP_SECONDS)
        if stop.is_set():
            return
        try:
            event_log.sweep()
        except Exception as exc:
            print(
                f"[tesserad] events sweep failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )


async def _embed_worker_loop(state: DaemonState, stop: asyncio.Event) -> None:
    """Periodically drain the embed queue until the daemon stops.

    Sleeps ``_EMBED_WORKER_IDLE_SECONDS`` between passes so a quiet
    vault does not spin the CPU. A single failing pass logs and retries
    rather than crashing the daemon — the worker is best-effort, and
    the vault's ``embed_status='failed'`` column + ``repair_embeds`` CLI
    cover the hard-failure case.
    """

    while not stop.is_set():
        try:
            stats = await embed_worker.run_pass(
                state.vault.connection,
                state.embedder,
                active_model_id=state.active_model_id,
                batch_size=_EMBED_WORKER_BATCH,
                event_log=state.event_log,
            )
            if stats.embedded == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_EMBED_WORKER_IDLE_SECONDS)
        except Exception as exc:
            # Single-line diagnostic to stderr; the daemon log captures
            # stderr so operators see the failure without the daemon
            # dying on every bad batch.
            print(
                f"[tesserad] embed_worker pass failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_EMBED_WORKER_IDLE_SECONDS)


def _make_tool_dispatcher(
    state: DaemonState,
) -> Callable[[VerifiedCapability, str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def _dispatch(
        verified: VerifiedCapability, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        return await dispatch_tool_call(state, verified, method, args)

    return _dispatch


def _make_control_dispatcher(
    state: DaemonState, stop: asyncio.Event, nonce_store: NonceStore
) -> Callable[[ControlRequest], Awaitable[dict[str, Any]]]:
    async def _dispatch(req: ControlRequest) -> dict[str, Any]:
        if req.method == "status":
            return {
                "ok": True,
                "vault_id": state.vault_id,
                "vault_path": str(state.vault_path),
                "active_model_id": state.active_model_id,
                "schema_version": state.vault.state.schema_version,
            }
        if req.method == "stop":
            stop.set()
            return {"stopping": True}
        if req.method == "ping":
            return {"pong": True}
        if req.method == "stash_bootstrap_nonce":
            return _stash_bootstrap_nonce(nonce_store, req.args)
        if req.method == "exchange_status":
            return {"pending": nonce_store.pending_count()}
        raise ControlError(f"unknown control method {req.method!r}")

    return _dispatch


def _stash_bootstrap_nonce(nonce_store: NonceStore, args: dict[str, Any]) -> dict[str, Any]:
    """Store a raw token under a fresh nonce; return the nonce to the CLI.

    The CLI mints the capability-token row itself (via direct vault
    access) and then calls this method so the running daemon can
    broker the ChatGPT URL-exchange handshake. The daemon never
    persists the raw token — it lives only in the in-memory nonce
    store until ChatGPT consumes the nonce or its 30-second TTL
    elapses.
    """

    raw_token = args.get("raw_token")
    if not isinstance(raw_token, str) or not raw_token:
        raise ControlError("raw_token required")
    entry = nonce_store.create(raw_token=raw_token, now_epoch=_now_epoch())
    return {"nonce": entry.nonce, "expires_at": entry.expires_at}


async def _shutdown(handles: DaemonHandles, *, socket_path: Path, pid_path: Path) -> None:
    handles.http_server.close()
    handles.control_server.close()
    await handles.http_server.wait_closed()
    await handles.control_server.wait_closed()
    handles.embed_task.cancel()
    handles.events_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await handles.embed_task
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await handles.events_task
    with contextlib.suppress(Exception):
        handles.event_log.close()
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()
    with contextlib.suppress(FileNotFoundError):
        pid_path.unlink()


def _write_pid_file(path: Path) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


# Re-export for callers that want to surface a distinct unknown-method
# error without importing the dispatch module directly.
__all__ = [
    "DaemonHandles",
    "UnknownMethodError",
    "run_daemon",
]
