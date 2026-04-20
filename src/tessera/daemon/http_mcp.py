"""HTTP MCP endpoint.

Minimal JSON-over-HTTP/1.1 framing on top of ``asyncio.start_server``
so the daemon can serve agent clients without a heavyweight web
framework dependency. The request shape mirrors the control plane:
``{"method": ..., "args": ...}`` → ``{"ok": bool, "result?": ...,
"error?": str}``.

Two cross-cutting concerns live here rather than in the route
handlers: the ``Origin`` header allowlist (rejects browser-driven
DNS-rebind attempts even though the socket is loopback-only) and the
``Authorization: Bearer <token>`` gate routed through
:func:`tessera.auth.tokens.verify_and_touch`. A request without a
valid token never reaches the tool dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import sqlcipher3

from tessera.auth.tokens import AuthDenied, VerifiedCapability, verify_and_touch

_MAX_HEADER_BYTES: Final[int] = 16 * 1024
_MAX_BODY_BYTES: Final[int] = 1 << 20  # 1 MiB
_READ_TIMEOUT_SECONDS: Final[float] = 30.0


class HttpMcpError(Exception):
    """Server-side failure with an HTTP status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


Dispatcher = Callable[[VerifiedCapability, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class HttpMcpRequest:
    method: str
    args: dict[str, Any]
    verified: VerifiedCapability


async def serve_http_mcp(
    *,
    host: str,
    port: int,
    allowed_origins: frozenset[str],
    conn: sqlcipher3.Connection,
    dispatch: Dispatcher,
    now_epoch_fn: Callable[[], int],
    ready: asyncio.Event | None = None,
) -> asyncio.AbstractServer:
    """Bind ``host:port`` and start serving until closed.

    ``allowed_origins`` is checked on every request with an ``Origin``
    header; requests without an Origin header are allowed (native
    clients do not set one), matching MCP spec expectations for local
    agent runtimes.
    """

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line, headers, body = await _read_request(reader)
        except HttpMcpError as exc:
            await _write_response(writer, exc.status, {"error": exc.message})
            return
        except (asyncio.IncompleteReadError, TimeoutError):
            with contextlib.suppress(ConnectionError):
                writer.close()
                await writer.wait_closed()
            return

        method, target, _ = request_line.split(" ", 2)
        if method != "POST":
            await _write_response(writer, 405, {"error": "POST only"})
            return
        if target != "/mcp":
            await _write_response(writer, 404, {"error": "unknown route"})
            return
        origin = headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            await _write_response(writer, 403, {"error": "origin not allowed"})
            return
        token_header = headers.get("authorization", "")
        if not token_header.lower().startswith("bearer "):
            await _write_response(writer, 401, {"error": "missing bearer token"})
            return
        raw_token = token_header[len("bearer ") :].strip()
        try:
            verified = verify_and_touch(conn, raw_token=raw_token, now_epoch=now_epoch_fn())
        except AuthDenied:
            await _write_response(writer, 401, {"error": "unauthenticated"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _write_response(writer, 400, {"error": "invalid json body"})
            return
        if not isinstance(payload, dict):
            await _write_response(writer, 400, {"error": "body must be a JSON object"})
            return
        mcp_method = payload.get("method")
        mcp_args = payload.get("args", {})
        if not isinstance(mcp_method, str) or not isinstance(mcp_args, dict):
            await _write_response(
                writer, 400, {"error": "method must be string, args must be object"}
            )
            return
        try:
            result = await dispatch(verified, mcp_method, mcp_args)
        except Exception as exc:
            # Error-class name only; message suppressed so internal
            # paths or data never leak to the HTTP client. The audit
            # log has the full trace for operators.
            await _write_response(writer, 500, {"error": f"internal:{type(exc).__name__}"})
            return
        await _write_response(writer, 200, {"ok": True, "result": result})

    server = await asyncio.start_server(_handle, host=host, port=port)
    if ready is not None:
        ready.set()
    return server


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, dict[str, str], bytes]:
    try:
        header_blob = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=_READ_TIMEOUT_SECONDS
        )
    except asyncio.LimitOverrunError as exc:
        raise HttpMcpError(431, "headers too large") from exc
    if len(header_blob) > _MAX_HEADER_BYTES:
        raise HttpMcpError(431, "headers too large")
    try:
        header_text = header_blob.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise HttpMcpError(400, "invalid header bytes") from exc
    lines = header_text.split("\r\n")
    request_line = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise HttpMcpError(400, "malformed header line")
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    content_length = int(headers.get("content-length", "0") or "0")
    if content_length < 0 or content_length > _MAX_BODY_BYTES:
        raise HttpMcpError(413, "body too large")
    body = (
        await asyncio.wait_for(reader.readexactly(content_length), timeout=_READ_TIMEOUT_SECONDS)
        if content_length
        else b""
    )
    return request_line, headers, body


async def _write_response(writer: asyncio.StreamWriter, status: int, body: dict[str, Any]) -> None:
    body_bytes = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {_status_text(status)}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("iso-8859-1")
    writer.write(headers + body_bytes)
    with contextlib.suppress(ConnectionError):
        await writer.drain()
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()


def _status_text(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        431: "Request Header Fields Too Large",
        500: "Internal Server Error",
    }.get(status, "HTTP")


__all__ = [
    "Dispatcher",
    "HttpMcpError",
    "HttpMcpRequest",
    "serve_http_mcp",
]
