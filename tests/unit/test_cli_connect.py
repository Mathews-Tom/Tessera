"""``tessera connect`` / ``tessera disconnect`` argparse wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.connectors import available_clients


@pytest.mark.unit
def test_connect_requires_client_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["connect"])
    err = capsys.readouterr().err
    assert "client" in err


@pytest.mark.unit
def test_connect_accepts_every_registered_client(tmp_path: Path) -> None:
    # The positional uses nargs="+" so a single client id parses into a
    # one-element list. The handler's _expand_clients collapses this to
    # the single client at dispatch time.
    parser = _build_parser()
    for client in available_clients():
        args = parser.parse_args(
            [
                "connect",
                client,
                "--vault",
                str(tmp_path / "v.db"),
                "--agent-id",
                "1",
            ]
        )
        assert args.client == [client]
        assert args.vault == tmp_path / "v.db"
        assert args.agent_id == 1


@pytest.mark.unit
def test_disconnect_accepts_every_registered_client(tmp_path: Path) -> None:
    parser = _build_parser()
    for client in available_clients():
        args = parser.parse_args(
            [
                "disconnect",
                client,
                "--vault",
                str(tmp_path / "v.db"),
            ]
        )
        assert args.client == [client]


@pytest.mark.unit
def test_connect_accepts_multiple_clients_and_all_meta(tmp_path: Path) -> None:
    # Regression for the multi-client + `all` meta expansion added
    # when the demo script was extended beyond Claude Desktop.
    parser = _build_parser()
    multi = parser.parse_args(
        [
            "connect",
            "claude-desktop",
            "claude-code",
            "--vault",
            str(tmp_path / "v.db"),
        ]
    )
    assert multi.client == ["claude-desktop", "claude-code"]
    sugar = parser.parse_args(["connect", "all", "--vault", str(tmp_path / "v.db")])
    assert sugar.client == ["all"]


@pytest.mark.unit
def test_connect_rejects_unknown_client(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "connect",
                "not-a-real-client",
                "--vault",
                "/tmp/v.db",
                "--agent-id",
                "1",
            ]
        )
    err = capsys.readouterr().err
    assert "invalid choice" in err or "argument" in err
