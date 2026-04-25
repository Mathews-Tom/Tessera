"""CLI argparse structure: subcommand dispatch + required-args."""

from __future__ import annotations

import pytest

import tessera
from tessera.cli.__main__ import _build_parser


@pytest.mark.unit
def test_parser_registers_every_subcommand() -> None:
    parser = _build_parser()
    subparsers_action = next(action for action in parser._actions if action.dest == "command")
    choices = subparsers_action.choices
    assert choices is not None
    expected = {
        "init",
        "daemon",
        "agents",
        "tokens",
        "capture",
        "recall",
        "show",
        "stats",
        "doctor",
        "models",
        "vault",
        "stdio",
    }
    assert expected <= set(choices)


@pytest.mark.unit
def test_tokens_create_requires_agent_and_client() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tokens", "create"])
    # Fully-specified invocation parses.
    parser.parse_args(
        [
            "tokens",
            "create",
            "--vault",
            "/tmp/v.db",
            "--agent-id",
            "1",
            "--client-name",
            "cli",
        ]
    )


@pytest.mark.unit
def test_init_requires_vault() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["init"])
    args = parser.parse_args(["init", "--vault", "/tmp/v.db"])
    assert args.command == "init"


@pytest.mark.unit
def test_daemon_start_fg_requires_vault() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["daemon", "start-fg"])
    args = parser.parse_args(["daemon", "start-fg", "--vault", "/tmp/v.db"])
    assert args.subcommand == "start-fg"


@pytest.mark.unit
def test_recall_requires_query() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["recall"])
    args = parser.parse_args(["recall", "what did I say about X"])
    assert args.query == "what did I say about X"
    assert args.k == 10


@pytest.mark.unit
def test_top_level_version_flag_prints_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "tessera" in output
    assert tessera.__version__ in output
