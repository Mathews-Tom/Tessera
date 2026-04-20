"""Unix-socket control plane: protocol, dispatch, error envelopes."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tessera.daemon.control import (
    ControlError,
    ControlRequest,
    call_control,
    serve_control_socket,
)


@pytest.fixture
def short_sock(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """AF_UNIX path cap on macOS is 104 chars; pytest tmp_path is too long."""

    del tmp_path_factory
    with tempfile.TemporaryDirectory(prefix="tess_", dir="/tmp") as tmp:
        yield Path(tmp) / "s.sock"


async def _echo_dispatcher(request: ControlRequest) -> dict[str, object]:
    if request.method == "fail":
        raise ControlError("intentional failure")
    if request.method == "boom":
        raise RuntimeError("internal bug with secrets")
    return {"method": request.method, "args": request.args}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_round_trip_echo(short_sock: Path) -> None:
    sock = short_sock
    server = await serve_control_socket(socket_path=sock, dispatch=_echo_dispatcher)
    try:
        assert oct(os.stat(sock).st_mode)[-3:] == "600"
        response = await call_control(sock, method="ping", args={"x": 1})
        assert response == {"method": "ping", "args": {"x": 1}}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_control_error_propagates(short_sock: Path) -> None:
    sock = short_sock
    server = await serve_control_socket(socket_path=sock, dispatch=_echo_dispatcher)
    try:
        with pytest.raises(ControlError, match="intentional failure"):
            await call_control(sock, method="fail")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_internal_error_suppresses_exception_message(short_sock: Path) -> None:
    """Non-ControlError exceptions land as ``internal:<ClassName>`` so
    secrets/paths in the original message never reach the client."""

    sock = short_sock
    server = await serve_control_socket(socket_path=sock, dispatch=_echo_dispatcher)
    try:
        with pytest.raises(ControlError) as exc:
            await call_control(sock, method="boom")
        assert "secrets" not in str(exc.value)
        assert str(exc.value).startswith("internal:")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_missing_socket_raises_connection_error(short_sock: Path) -> None:
    with pytest.raises(ConnectionError):
        await call_control(short_sock.parent / "no.sock", method="ping")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_malformed_json_returns_error_envelope(short_sock: Path) -> None:
    """The raw line protocol: bad JSON yields {"ok": false, "error": ...}."""

    sock = short_sock
    server = await serve_control_socket(socket_path=sock, dispatch=_echo_dispatcher)
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b"this is not json\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        payload = json.loads(line.decode("utf-8"))
        assert payload["ok"] is False
        assert "invalid json" in payload["error"]
    finally:
        server.close()
        await server.wait_closed()
