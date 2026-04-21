"""CLI vault-ops end-to-end: init → agents → tokens → doctor.

Exercises the CLI handler functions directly so the tests remain in-
process (no subprocess), which keeps runtime under a second and lets
pytest capture stdout/stderr cleanly.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tessera.cli.__main__ import _build_parser


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """Short-path tmpdir so AF_UNIX paths under ``/tmp`` stay valid."""

    with tempfile.TemporaryDirectory(prefix="tess_", dir="/tmp") as tmp:
        yield Path(tmp)


@pytest.mark.integration
def test_init_creates_vault_and_agent(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "correct horse battery staple")
    vault = short_tmp / "v.db"
    parser = _build_parser()
    args = parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"])
    rc = args.handler(args)
    assert rc == 0
    assert vault.exists()
    assert (vault.parent / (vault.name + ".salt")).exists()
    out = capsys.readouterr().out
    assert "initialised vault" in out
    assert "agent: default" in out


@pytest.mark.integration
def test_init_refuses_to_overwrite_existing(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "p")
    vault = short_tmp / "v.db"
    vault.write_bytes(b"")  # precreate
    parser = _build_parser()
    args = parser.parse_args(["init", "--vault", str(vault)])
    assert args.handler(args) == 1


@pytest.mark.integration
def test_init_requires_passphrase(short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["init", "--vault", str(short_tmp / "v.db")])
    assert args.handler(args) == 1


@pytest.mark.integration
def test_agents_create_and_list(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "xyz")
    vault = short_tmp / "v.db"
    parser = _build_parser()
    init_args = parser.parse_args(["init", "--vault", str(vault), "--agent-name", "root"])
    init_args.handler(init_args)
    capsys.readouterr()
    create_args = parser.parse_args(["agents", "create", "--vault", str(vault), "--name", "second"])
    create_args.handler(create_args)
    created_out = capsys.readouterr().out.strip()
    assert len(created_out) == 26  # ULID
    list_args = parser.parse_args(["agents", "list", "--vault", str(vault)])
    list_args.handler(list_args)
    listed = capsys.readouterr().out
    assert "root" in listed
    assert "second" in listed


@pytest.mark.integration
def test_tokens_create_list_revoke_round_trip(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "xyz")
    vault = short_tmp / "v.db"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "a"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "a"])
    )
    capsys.readouterr()
    create_args = parser.parse_args(
        [
            "tokens",
            "create",
            "--vault",
            str(vault),
            "--agent-id",
            "1",
            "--client-name",
            "cli",
            "--read",
            "style",
        ]
    )
    rc = create_args.handler(create_args)
    assert rc == 0
    create_out = capsys.readouterr().out
    assert "access_token: tessera_session_" in create_out
    assert "refresh_token: tessera_session_" in create_out
    list_args = parser.parse_args(["tokens", "list", "--vault", str(vault)])
    list_args.handler(list_args)
    listed = capsys.readouterr().out
    assert "session" in listed
    revoke_args = parser.parse_args(["tokens", "revoke", "--vault", str(vault), "--token-id", "1"])
    rc = revoke_args.handler(revoke_args)
    assert rc == 0
    # Second revoke is a no-op and returns non-zero so operators learn
    # the target was already revoked.
    rc = revoke_args.handler(revoke_args)
    assert rc == 1


@pytest.mark.integration
def test_doctor_runs_without_vault(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(short_tmp))
    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["doctor"])
    # Exit code is 0 or 1 depending on whether ollama / bind checks pass.
    rc = args.handler(args)
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "verdict:" in out
    assert "[WARN] vault" in out or "[OK]  vault" in out or "[FAIL]" in out
