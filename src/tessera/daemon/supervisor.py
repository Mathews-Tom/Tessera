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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tessera.adapters.st_reranker import SentenceTransformersReranker
from tessera.auth.tokens import VerifiedCapability
from tessera.daemon.config import DaemonConfig
from tessera.daemon.control import ControlError, ControlRequest, serve_control_socket
from tessera.daemon.dispatch import UnknownMethodError, dispatch_tool_call
from tessera.daemon.http_mcp import serve_http_mcp
from tessera.daemon.state import DaemonState, open_vault_for_daemon, resolve_embedder
from tessera.retrieval import embed_worker
from tessera.vault.encryption import ProtectedKey, derive_key, load_salt

_EMBED_WORKER_BATCH = 128
_EMBED_WORKER_IDLE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class DaemonHandles:
    """Running daemon: what the supervisor started, what the shutdown releases."""

    state: DaemonState
    http_server: asyncio.AbstractServer
    control_server: asyncio.AbstractServer
    embed_task: asyncio.Task[None]


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
            embedder, active_model_id, vec_table = resolve_embedder(
                vault.connection, ollama_host=config.ollama_host
            )
            reranker = SentenceTransformersReranker(model_name=config.reranker_model)
            state = DaemonState(
                vault_path=config.vault_path,
                vault=vault,
                embedder=embedder,
                reranker=reranker,
                active_model_id=active_model_id,
                vec_table=vec_table,
                vault_id=vault.state.vault_id,
            )
            stop = stop_event or asyncio.Event()
            _install_signal_handlers(stop)
            http_server = await serve_http_mcp(
                host=config.http_host,
                port=config.http_port,
                allowed_origins=config.allowed_origins,
                conn=vault.connection,
                dispatch=_make_tool_dispatcher(state),
                now_epoch_fn=_now_epoch,
            )
            control_server = await serve_control_socket(
                socket_path=config.socket_path,
                dispatch=_make_control_dispatcher(state, stop),
            )
            embed_task = asyncio.create_task(
                _embed_worker_loop(state, stop), name="tessera.embed_worker"
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


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


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
    state: DaemonState, stop: asyncio.Event
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
        raise ControlError(f"unknown control method {req.method!r}")

    return _dispatch


async def _shutdown(handles: DaemonHandles, *, socket_path: Path, pid_path: Path) -> None:
    handles.http_server.close()
    handles.control_server.close()
    await handles.http_server.wait_closed()
    await handles.control_server.wait_closed()
    handles.embed_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await handles.embed_task
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
