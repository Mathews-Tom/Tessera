"""CLI network-facing commands with mocked transport.

Covers ``tessera {capture,recall,show,stats}`` (HTTP MCP) and
``tessera daemon {stop,status}`` (Unix control) via monkeypatched
client calls — no live daemon required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tessera.cli import daemon_cmd
from tessera.cli.__main__ import _build_parser
from tessera.daemon.control import ControlError


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.unit
def test_capture_sends_bearer_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def _fake_post(
        url: str, *, headers: dict[str, str], json: Any, timeout: float
    ) -> _DummyResponse:
        seen["url"] = url
        seen["headers"] = headers
        seen["body"] = json
        return _DummyResponse(200, {"ok": True, "result": {"external_id": "01X"}})

    monkeypatch.setattr("tessera.cli._http.httpx.post", _fake_post)
    monkeypatch.setenv("TESSERA_TOKEN", "tessera_session_AAAAAAAAAAAAAAAAAAAAAAAA")
    parser = _build_parser()
    args = parser.parse_args(["capture", "hello world", "--facet-type", "project"])
    assert args.handler(args) == 0
    assert seen["headers"]["Authorization"].startswith("Bearer tessera_session_")
    assert seen["body"]["method"] == "capture"
    out = capsys.readouterr().out
    assert "01X" in out


@pytest.mark.unit
def test_capture_fails_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSERA_TOKEN", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["capture", "hi", "--facet-type", "style"])
    assert args.handler(args) == 1


@pytest.mark.unit
def test_capture_requires_facet_type(capsys: pytest.CaptureFixture[str]) -> None:
    # Per ADR 0010 every capture is an explicit user choice between the
    # five v0.1 facet types; argparse must refuse to produce a default.
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["capture", "hi"])
    err = capsys.readouterr().err
    assert "--facet-type" in err


@pytest.mark.unit
def test_capture_rejects_retired_facet_type(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["capture", "hi", "--facet-type", "episodic"])
    err = capsys.readouterr().err
    assert "invalid choice" in err or "argument" in err


@pytest.mark.unit
def test_stats_surfaces_http_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_post(url: str, **kwargs: Any) -> _DummyResponse:
        del url, kwargs
        return _DummyResponse(401, {"error": "unauthenticated"})

    monkeypatch.setattr("tessera.cli._http.httpx.post", _fake_post)
    monkeypatch.setenv("TESSERA_TOKEN", "t")
    parser = _build_parser()
    args = parser.parse_args(["stats"])
    assert args.handler(args) == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err


@pytest.mark.unit
def test_recall_unknown_host_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    def _boom(url: str, **kwargs: Any) -> _DummyResponse:
        del kwargs
        raise _httpx.ConnectError("nope")

    monkeypatch.setattr("tessera.cli._http.httpx.post", _boom)
    monkeypatch.setenv("TESSERA_TOKEN", "t")
    parser = _build_parser()
    args = parser.parse_args(["recall", "q"])
    assert args.handler(args) == 1


def _patch_socket_path_to_existing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Route ``resolve_config`` at an existing tmp_path socket stub.

    _cmd_status and _cmd_stop short-circuit on
    ``config.socket_path.exists()`` — tests that assert on the
    control-plane round-trip need the pre-check to pass so the
    monkeypatched ``call_control`` is reached.
    """

    import dataclasses

    from tessera.daemon.config import resolve_config

    socket_path = tmp_path / "tessera.sock"
    socket_path.touch()
    real_config = resolve_config()
    fake_config = dataclasses.replace(real_config, socket_path=socket_path)
    monkeypatch.setattr(daemon_cmd, "resolve_config", lambda: fake_config)
    return socket_path


@pytest.mark.unit
def test_daemon_status_prints_control_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _patch_socket_path_to_existing(monkeypatch, tmp_path)

    async def _fake_call(
        path: Any, *, method: str, args: Any = None, timeout_seconds: float = 10.0
    ) -> dict[str, Any]:
        del path, args, timeout_seconds
        assert method == "status"
        return {
            "vault_id": "01VAULT",
            "vault_path": "/tmp/v.db",
            "schema_version": 1,
            "active_model_id": 7,
        }

    monkeypatch.setattr(daemon_cmd, "call_control", _fake_call)
    parser = _build_parser()
    args = parser.parse_args(["daemon", "status"])
    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "vault_id: 01VAULT" in out
    assert "active_model_id: 7" in out


@pytest.mark.unit
def test_daemon_status_reports_missing_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Point at a socket path that does NOT exist. _cmd_status's
    # pre-check should return exit 1 with a "daemon not running"
    # message without ever dialing call_control.
    import dataclasses

    from tessera.daemon.config import resolve_config

    real_config = resolve_config()
    fake_config = dataclasses.replace(real_config, socket_path=tmp_path / "ghost.sock")
    monkeypatch.setattr(daemon_cmd, "resolve_config", lambda: fake_config)
    parser = _build_parser()
    args = parser.parse_args(["daemon", "status"])
    assert args.handler(args) == 1


@pytest.mark.unit
def test_daemon_stop_returns_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_socket_path_to_existing(monkeypatch, tmp_path)

    async def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"stopping": True}

    monkeypatch.setattr(daemon_cmd, "call_control", _ok)
    parser = _build_parser()
    args = parser.parse_args(["daemon", "stop"])
    assert args.handler(args) == 0


@pytest.mark.unit
def test_daemon_stop_is_idempotent_when_already_stopped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Fresh behaviour: stop against an absent socket returns 0 with a
    # "daemon already stopped" info line, matching systemctl/launchctl
    # conventions. Before the fix, this path returned exit 1 with a
    # "control socket not found" ERROR line that confused idempotent
    # teardown scripts.
    import dataclasses

    from tessera.daemon.config import resolve_config

    real_config = resolve_config()
    fake_config = dataclasses.replace(real_config, socket_path=tmp_path / "ghost.sock")
    monkeypatch.setattr(daemon_cmd, "resolve_config", lambda: fake_config)
    parser = _build_parser()
    args = parser.parse_args(["daemon", "stop"])
    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "already stopped" in out


@pytest.mark.unit
def test_daemon_stop_reports_control_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_socket_path_to_existing(monkeypatch, tmp_path)

    async def _err(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise ControlError("refused")

    monkeypatch.setattr(daemon_cmd, "call_control", _err)
    parser = _build_parser()
    args = parser.parse_args(["daemon", "stop"])
    assert args.handler(args) == 1
