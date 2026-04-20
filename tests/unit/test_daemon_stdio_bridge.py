"""Stdio MCP bridge stub: structured refusal on stdin line."""

from __future__ import annotations

import io
import json

import pytest

from tessera.daemon import stdio_bridge


@pytest.mark.unit
def test_stub_returns_structured_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"method":"ping"}\n'))
    rc = stdio_bridge.run_stub()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "not implemented" in payload["error"]


@pytest.mark.unit
def test_stub_handles_malformed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json\n"))
    rc = stdio_bridge.run_stub()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "malformed" in payload["error"]


@pytest.mark.unit
def test_stub_empty_stdin_is_noop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = stdio_bridge.run_stub()
    assert rc == 0
    assert capsys.readouterr().out == ""
