"""Unix-socket control plane for CLI-to-daemon commands.

One JSON request, one JSON response, both newline-terminated. The
protocol is deliberately flat — a ``method`` name and optional ``args``
map, mirroring the internal Python dispatch shape — so a Python client
(the CLI) and a human with netcat can both drive it.

Authentication is filesystem permission: the socket file is created
with mode 0600 in ``$XDG_RUNTIME_DIR/tessera`` (or ``~/.tessera/run``
when XDG is absent). Anyone who can read the user's runtime dir can
already read the vault file; adding a capability-token check here
would not change the blast radius.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONTROL_SOCKET_MODE = 0o600
_READ_LIMIT_BYTES = 1 << 20  # 1 MiB cap per control request.


class ControlError(Exception):
    """Server-side failure; maps to ``{"ok": False, "error": ...}`` response."""


@dataclass(frozen=True, slots=True)
class ControlRequest:
    method: str
    args: dict[str, Any]


Handler = Callable[[ControlRequest], Awaitable[dict[str, Any]]]


async def serve_control_socket(
    *,
    socket_path: Path,
    dispatch: Handler,
    ready: asyncio.Event | None = None,
) -> asyncio.AbstractServer:
    """Bind ``socket_path`` at mode 0600 and serve until closed.

    Returns the started :class:`asyncio.AbstractServer` so the caller
    can hold its lifecycle; the socket file is unlinked before bind so
    a stale file from a crashed prior daemon does not block startup.
    """

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
        except (TimeoutError, asyncio.IncompleteReadError):
            writer.close()
            await writer.wait_closed()
            return
        if len(line) > _READ_LIMIT_BYTES:
            await _reply(writer, {"ok": False, "error": "request too large"})
            return
        response = await _dispatch_one(line, dispatch)
        await _reply(writer, response)

    server = await asyncio.start_unix_server(_handle, path=str(socket_path))
    os.chmod(socket_path, _CONTROL_SOCKET_MODE)
    if ready is not None:
        ready.set()
    return server


async def _dispatch_one(line: bytes, dispatch: Handler) -> dict[str, Any]:
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"invalid json: {type(exc).__name__}"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request must be a JSON object"}
    method = payload.get("method")
    args = payload.get("args", {})
    if not isinstance(method, str) or not isinstance(args, dict):
        return {"ok": False, "error": "request must have string method and dict args"}
    try:
        result = await dispatch(ControlRequest(method=method, args=args))
    except ControlError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # Unexpected errors surface the exception type only; the
        # message may contain internal paths or values that the CLI
        # caller should not see verbatim.
        return {"ok": False, "error": f"internal:{type(exc).__name__}"}
    return {"ok": True, "result": result}


async def _reply(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    with contextlib.suppress(ConnectionError):
        await writer.drain()
    writer.close()
    await writer.wait_closed()


async def call_control(
    socket_path: Path,
    *,
    method: str,
    args: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Client helper: round-trip one request.

    Returns the parsed response dict. Raises :class:`ConnectionError`
    when the socket is absent or closed; raises :class:`ControlError`
    when the server returns ``ok=False``.
    """

    if not socket_path.exists():
        raise ConnectionError(f"control socket not found at {socket_path}")
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)),
        timeout=timeout_seconds,
    )
    try:
        request = json.dumps({"method": method, "args": args or {}}) + "\n"
        writer.write(request.encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=timeout_seconds)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ControlError("malformed response (not an object)")
    if not payload.get("ok", False):
        raise ControlError(str(payload.get("error", "unknown error")))
    result = payload.get("result", {})
    if not isinstance(result, dict):
        raise ControlError("malformed response (result is not an object)")
    return result


__all__ = [
    "ControlError",
    "ControlRequest",
    "Handler",
    "call_control",
    "serve_control_socket",
]
