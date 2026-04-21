"""Doctor check matrix: aggregation, verdict, per-check behaviour."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from tessera.daemon.config import resolve_config
from tessera.daemon.doctor import DoctorStatus, run_all


@pytest.mark.asyncio
@pytest.mark.unit
async def test_doctor_without_vault_returns_warn_on_vault_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    config = resolve_config(http_port=_pick_free_port(), ollama_host="http://127.0.0.1:65500")
    report = await run_all(config)
    names = {r.name for r in report.results}
    assert "vault" in names
    vault_result = next(r for r in report.results if r.name == "vault")
    assert vault_result.status is DoctorStatus.WARN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_verdict_is_error_when_any_error_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Reserve the port so bind_address fails.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        port = sock.getsockname()[1]
        config = resolve_config(http_port=port, ollama_host="http://127.0.0.1:65500")
        report = await run_all(config)
    finally:
        sock.close()
    assert any(r.status is DoctorStatus.ERROR for r in report.results)
    assert report.verdict is DoctorStatus.ERROR


@pytest.mark.asyncio
@pytest.mark.unit
async def test_doctor_ollama_unreachable_is_warn_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Use a closed port for ollama: the check must downgrade to WARN,
    # not ERROR — the user may be intentionally offline.
    config = resolve_config(http_port=_pick_free_port(), ollama_host="http://127.0.0.1:65500")

    class _NeverReachable(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope", request=request)

    client = httpx.AsyncClient(base_url=config.ollama_host, transport=_NeverReachable())
    try:
        report = await run_all(config, httpx_client=client)
    finally:
        await client.aclose()
    ollama_result = next(r for r in report.results if r.name == "ollama")
    assert ollama_result.status is DoctorStatus.WARN


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)
