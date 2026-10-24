"""``tessera connect`` argument validation and safe administrative selection."""

from __future__ import annotations

import pytest

from tessera.cli.__main__ import _build_parser


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


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["connect", "all", "--recover"],
        ["connect", "omp", "pi", "--recover", "--path", "/tmp/shared.json"],
        ["connect", "chatgpt", "--status"],
        ["connect", "omp", "--recover", "--agent-id", "1"],
        ["connect", "omp", "--recover", "--token-ttl-days", "90"],
    ],
)
def test_managed_modes_reject_unsafe_selection_before_any_io(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tessera.cli import connect_cmd

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("unsafe selection must fail before socket or vault access")

    monkeypatch.setattr(connect_cmd, "open_vault", forbidden)
    monkeypatch.setattr(connect_cmd, "call_control", forbidden)
    args = _build_parser().parse_args(argv)
    assert args.handler(args) == 1


@pytest.mark.unit
def test_managed_status_and_recovery_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["connect", "omp", "--status", "--recover"])
