"""HTTP MCP endpoint.

Minimal JSON-over-HTTP/1.1 framing on top of ``asyncio.start_server``
so the daemon can serve agent clients without a heavyweight web
framework dependency. The request shape mirrors the control plane:
``{"method": ..., "args": ...}`` → ``{"ok": true, "result": ...}``
or ``{"ok": false, "error": {"code": "...", "message": "..."}}``.

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
from tessera.daemon.dispatch import UnknownMethodError
from tessera.daemon.exchange import NonceStore, UnknownNonceError
from tessera.mcp_surface import tools as mcp_tools

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


@dataclass(frozen=True, slots=True)
class DispatchError:
    """Wire-shape failure: HTTP status comes from the surrounding outcome."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Authenticated dispatch result — one shape, two transports.

    ``status`` is the HTTP status the caller writes. ``result`` is the
    dispatcher's return value on success and is ``None`` on failure;
    ``error`` is the inverse. Exactly one of (``result``, ``error``)
    is non-None per outcome — enforced at construction sites, not
    re-checked at the wire layer.
    """

    status: int
    result: dict[str, Any] | None = None
    error: DispatchError | None = None


async def _authenticate_and_dispatch(
    *,
    headers: dict[str, str],
    method: str,
    args: dict[str, Any],
    allowed_origins: frozenset[str],
    conn: sqlcipher3.Connection,
    dispatch: Dispatcher,
    now_epoch_fn: Callable[[], int],
) -> DispatchOutcome:
    """Apply the cross-cutting Origin + bearer-token gates and dispatch.

    Shared by ``/mcp`` (JSON-RPC envelope) and ``/api/v1/*`` (lean REST).
    The two transports differ only in how they parse ``method``/``args``
    out of the request and how they shape the response — auth, scope
    denial mapping, and dispatcher error classification are identical
    on both surfaces, so they live here.
    """

    origin = headers.get("origin")
    if origin is not None and origin not in allowed_origins:
        return DispatchOutcome(
            status=403,
            error=DispatchError(code="scope_denied", message="origin not allowed"),
        )
    token_header = headers.get("authorization", "")
    if not token_header.lower().startswith("bearer "):
        return DispatchOutcome(
            status=401,
            error=DispatchError(code="invalid_input", message="missing bearer token"),
        )
    raw_token = token_header[len("bearer ") :].strip()
    try:
        verified = verify_and_touch(conn, raw_token=raw_token, now_epoch=now_epoch_fn())
    except AuthDenied:
        return DispatchOutcome(
            status=401,
            error=DispatchError(code="scope_denied", message="unauthenticated"),
        )
    try:
        result = await dispatch(verified, method, args)
    except mcp_tools.ValidationError as exc:
        return DispatchOutcome(
            status=400, error=DispatchError(code="invalid_input", message=str(exc))
        )
    except mcp_tools.ScopeDenied as exc:
        return DispatchOutcome(
            status=403, error=DispatchError(code="scope_denied", message=str(exc))
        )
    except UnknownMethodError as exc:
        return DispatchOutcome(
            status=400, error=DispatchError(code="unknown_method", message=str(exc))
        )
    except mcp_tools.StorageError as exc:
        return DispatchOutcome(
            status=500, error=DispatchError(code="storage_error", message=str(exc))
        )
    except Exception as exc:
        # Error-class name only; message suppressed so internal paths
        # or data never leak to the HTTP client. The audit log has
        # the full trace for operators.
        return DispatchOutcome(
            status=500,
            error=DispatchError(code="internal_error", message=type(exc).__name__),
        )
    return DispatchOutcome(status=200, result=result)


async def serve_http_mcp(
    *,
    host: str,
    port: int,
    allowed_origins: frozenset[str],
    conn: sqlcipher3.Connection,
    dispatch: Dispatcher,
    now_epoch_fn: Callable[[], int],
    nonce_store: NonceStore | None = None,
    ready: asyncio.Event | None = None,
) -> asyncio.AbstractServer:
    """Bind ``host:port`` and start serving until closed.

    ``allowed_origins`` is checked on every request with an ``Origin``
    header; requests without an Origin header are allowed (native
    clients do not set one), matching MCP spec expectations for local
    agent runtimes.

    ``nonce_store`` wires the ChatGPT Developer Mode bootstrap-exchange
    endpoint at ``POST /mcp/exchange``. When absent, the route returns
    404 and the daemon cannot broker ChatGPT handshakes.
    """

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line, headers, body = await _read_request(reader)
        except HttpMcpError as exc:
            await _write_error(writer, exc.status, "invalid_input", exc.message)
            return
        except (asyncio.IncompleteReadError, TimeoutError):
            with contextlib.suppress(ConnectionError):
                writer.close()
                await writer.wait_closed()
            return

        try:
            http_method, target, _ = request_line.split(" ", 2)
        except ValueError:
            await _write_error(writer, 400, "invalid_input", "malformed request line")
            return
        if http_method != "POST":
            await _write_error(writer, 405, "invalid_input", "POST only")
            return
        if target == "/mcp/exchange":
            await _route_exchange(writer, headers, body, nonce_store, now_epoch_fn)
            return
        if target != "/mcp":
            await _write_error(writer, 404, "unknown_method", "unknown route")
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _write_error(writer, 400, "invalid_input", "invalid json body")
            return
        if not isinstance(payload, dict):
            await _write_error(writer, 400, "invalid_input", "body must be a JSON object")
            return
        mcp_method = payload.get("method")
        mcp_args = payload.get("args", {})
        if not isinstance(mcp_method, str) or not isinstance(mcp_args, dict):
            await _write_error(
                writer, 400, "invalid_input", "method must be string, args must be object"
            )
            return
        outcome = await _authenticate_and_dispatch(
            headers=headers,
            method=mcp_method,
            args=mcp_args,
            allowed_origins=allowed_origins,
            conn=conn,
            dispatch=dispatch,
            now_epoch_fn=now_epoch_fn,
        )
        if outcome.error is not None:
            await _write_error(writer, outcome.status, outcome.error.code, outcome.error.message)
            return
        await _write_response(writer, 200, {"ok": True, "result": outcome.result})

    server = await asyncio.start_server(_handle, host=host, port=port)
    if ready is not None:
        ready.set()
    return server


async def _route_exchange(
    writer: asyncio.StreamWriter,
    headers: dict[str, str],
    body: bytes,
    nonce_store: NonceStore | None,
    now_epoch_fn: Callable[[], int],
) -> None:
    """Handle POST /mcp/exchange — ChatGPT Dev Mode bootstrap.

    The endpoint is intentionally unauthenticated at the bearer-token
    level (the nonce itself is the single-use credential). It still
    enforces the Origin allowlist so a browser cannot drive the call
    from a page the user opens in a compromised context. The body
    shape is ``{"nonce": "..."}``; success returns
    ``{"ok": True, "token": "tessera_session_..."}``; every failure
    shape is indistinguishable to prevent nonce-probing.
    """

    if nonce_store is None:
        await _write_response(writer, 404, {"error": "unknown route"})
        return
    # The exchange endpoint uses a narrower Origin allowlist than
    # /mcp: only the null origin (native clients, curl from the
    # CLI) is accepted. A browser-driven request would carry an
    # ``Origin`` header pointing at a real page, which the client
    # could not control — so any non-null Origin is rejected here
    # even if ``allowed_origins`` would accept it on /mcp.
    origin = headers.get("origin")
    if origin not in (None, "null"):
        await _write_response(writer, 403, {"error": "origin not allowed"})
        return
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await _write_response(writer, 400, {"error": "invalid json body"})
        return
    if not isinstance(payload, dict):
        await _write_response(writer, 400, {"error": "body must be a JSON object"})
        return
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        await _write_response(writer, 400, {"error": "nonce required"})
        return
    try:
        raw_token = nonce_store.consume(nonce=nonce, now_epoch=now_epoch_fn())
    except UnknownNonceError:
        # One error shape for every failure so callers cannot
        # distinguish "never issued" from "expired" from "already
        # consumed" via response timing or content.
        await _write_response(writer, 401, {"error": "nonce rejected"})
        return
    await _write_response(writer, 200, {"ok": True, "token": raw_token})


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
    try:
        content_length = int(headers.get("content-length", "0") or "0")
    except ValueError as exc:
        raise HttpMcpError(400, "invalid content-length") from exc
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


async def _write_error(
    writer: asyncio.StreamWriter,
    status: int,
    code: str,
    message: str,
) -> None:
    await _write_response(
        writer,
        status,
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        },
    )


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
    "DispatchError",
    "DispatchOutcome",
    "Dispatcher",
    "HttpMcpError",
    "HttpMcpRequest",
    "serve_http_mcp",
]
