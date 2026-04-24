"""Unit tests for the Rich-powered CLI UI helpers.

Covers the contracts the rest of the CLI depends on:
- Status tokens (``OK``, ``WARN``, ``ERROR``) survive the emoji wrapping so
  ``tessera doctor | grep ERROR`` keeps working.
- :func:`raw` emits its argument with no formatting so machine outputs
  (ULIDs, tokens) pipe cleanly.
- :func:`kv_panel` and :func:`report_table` render without raising in
  non-TTY mode (capsys intercepts make Rich drop to plain text).
"""

from __future__ import annotations

import pytest

from tessera.cli._ui import (
    EMOJI,
    error,
    info,
    kv_panel,
    raw,
    report_table,
    success,
    warn,
)


def test_success_prefixes_tick_emoji(capsys: pytest.CaptureFixture[str]) -> None:
    success("vault unlocked")
    out = capsys.readouterr().out
    assert EMOJI["ok"] in out
    assert "vault unlocked" in out


def test_warn_contains_literal_token(capsys: pytest.CaptureFixture[str]) -> None:
    warn("no tokens")
    out = capsys.readouterr().out
    # grep-stable word "WARN" must appear even after Rich rendering.
    assert "WARN" in out
    assert "no tokens" in out


def test_error_goes_to_stderr_and_contains_error_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error("port busy")
    captured = capsys.readouterr()
    # The literal ERROR token stays grep-stable; stderr is the channel.
    assert "ERROR" in captured.err
    assert "port busy" in captured.err
    # Nothing on stdout so pipelines stay clean.
    assert captured.out == ""


def test_info_rendered_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    info("daemon idle")
    captured = capsys.readouterr()
    assert "daemon idle" in captured.out


def test_raw_emits_exact_value(capsys: pytest.CaptureFixture[str]) -> None:
    # The ULID-length ID test exercises the same contract scripts rely
    # on: `id=$(tessera agents create ...)` gets exactly the id.
    raw("01HVGKZYAK5W2M5W3F4G5H6J7K")
    out = capsys.readouterr().out
    assert out.strip() == "01HVGKZYAK5W2M5W3F4G5H6J7K"


def test_kv_panel_renders_key_value_pairs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kv_panel("vault", {"vault_id": "01ABC", "schema": "v2"})
    out = capsys.readouterr().out
    assert "vault" in out
    assert "vault_id" in out
    assert "01ABC" in out
    assert "schema" in out


def test_report_table_renders_rows(capsys: pytest.CaptureFixture[str]) -> None:
    from tessera.cli._ui import console

    table = report_table("agents", ["external_id", "name"])
    table.add_row("01HX", "daisy")
    table.add_row("01HY", "alice")
    console.print(table)
    out = capsys.readouterr().out
    assert "agents" in out
    assert "daisy" in out
    assert "alice" in out
    assert "external_id" in out
