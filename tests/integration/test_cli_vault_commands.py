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
def test_tokens_create_accepts_demo_script_flags(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Regression guard for the exact invocation documented in
    # docs/user-demo/demo-script.md §Stage 0: --read-scope /
    # --write-scope as comma-separated lists, no --agent-id (single
    # default agent is auto-selected). Without this test the demo
    # script can silently drift out of sync with the CLI surface.
    monkeypatch.setenv("TESSERA_PASSPHRASE", "demo")
    vault = short_tmp / "v.db"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"])
    )
    capsys.readouterr()
    create_args = parser.parse_args(
        [
            "tokens",
            "create",
            "--vault",
            str(vault),
            "--client-name",
            "demo",
            "--token-class",
            "session",
            "--read-scope",
            "identity,preference,workflow,project,style",
            "--write-scope",
            "identity,preference,workflow,project,style",
        ]
    )
    rc = create_args.handler(create_args)
    assert rc == 0
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert "tessera_session_" in combined


@pytest.mark.integration
def test_connect_auto_selects_single_agent(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Regression guard for the demo-script invocation:
    #   tessera connect claude-desktop --vault X --passphrase Y
    # (no --agent-id). The handler must auto-select the sole agent
    # that `tessera init` created, mint the token, and write the
    # client config at --path.
    monkeypatch.setenv("TESSERA_PASSPHRASE", "demo")
    vault = short_tmp / "v.db"
    config_path = short_tmp / "claude_desktop_config.json"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"])
    )
    capsys.readouterr()
    connect_args = parser.parse_args(
        [
            "connect",
            "claude-desktop",
            "--vault",
            str(vault),
            "--path",
            str(config_path),
        ]
    )
    rc = connect_args.handler(connect_args)
    assert rc == 0
    assert config_path.is_file()
    # The config file should now contain a Tessera MCP entry.
    assert "tessera" in config_path.read_text()


@pytest.mark.integration
def test_connect_fails_loud_with_multiple_agents(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --agent-id auto-select on `tessera connect` must refuse to guess
    # when the vault holds more than one agent, same contract as
    # `tessera tokens create`.
    monkeypatch.setenv("TESSERA_PASSPHRASE", "demo")
    vault = short_tmp / "v.db"
    config_path = short_tmp / "claude_desktop_config.json"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "first"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "first"])
    )
    capsys.readouterr()
    parser.parse_args(["agents", "create", "--vault", str(vault), "--name", "second"]).handler(
        parser.parse_args(["agents", "create", "--vault", str(vault), "--name", "second"])
    )
    capsys.readouterr()
    connect_args = parser.parse_args(
        [
            "connect",
            "claude-desktop",
            "--vault",
            str(vault),
            "--path",
            str(config_path),
        ]
    )
    rc = connect_args.handler(connect_args)
    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "2 agents" in combined
    assert "--agent-id" in combined
    assert not config_path.exists()


@pytest.mark.integration
def test_connect_all_writes_every_file_based_client(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `tessera connect all` expands to the four file-based clients
    # (claude-desktop, claude-code, cursor, codex). ChatGPT is not in
    # the `all` meta because its handler uses the URL-exchange flow.
    monkeypatch.setenv("TESSERA_PASSPHRASE", "demo")
    monkeypatch.setenv("HOME", str(short_tmp))
    vault = short_tmp / "v.db"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"])
    )
    capsys.readouterr()
    connect_args = parser.parse_args(["connect", "all", "--vault", str(vault)])
    rc = connect_args.handler(connect_args)
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # Every file-based client name surfaces in the output.
    for name in ("Claude Desktop", "Claude Code", "Cursor", "Codex"):
        assert name in combined
    # ChatGPT is deliberately absent from the `all` expansion.
    assert "ChatGPT" not in combined


@pytest.mark.integration
def test_connect_accepts_multiple_explicit_clients(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The positional accepts one or more client ids. Duplicates collapse
    # to the first occurrence so `connect claude-desktop claude-desktop`
    # writes the config once, not twice.
    monkeypatch.setenv("TESSERA_PASSPHRASE", "demo")
    monkeypatch.setenv("HOME", str(short_tmp))
    vault = short_tmp / "v.db"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"])
    )
    capsys.readouterr()
    connect_args = parser.parse_args(
        ["connect", "claude-desktop", "claude-code", "--vault", str(vault)]
    )
    rc = connect_args.handler(connect_args)
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Claude Desktop" in combined
    assert "Claude Code" in combined


@pytest.mark.integration
def test_tokens_create_fails_loud_with_multiple_agents(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --agent-id auto-select refuses to guess when the vault holds more
    # than one agent. The error message must surface the agent ids so
    # the operator can pick one explicitly.
    monkeypatch.setenv("TESSERA_PASSPHRASE", "demo")
    vault = short_tmp / "v.db"
    parser = _build_parser()
    parser.parse_args(["init", "--vault", str(vault), "--agent-name", "first"]).handler(
        parser.parse_args(["init", "--vault", str(vault), "--agent-name", "first"])
    )
    capsys.readouterr()
    parser.parse_args(["agents", "create", "--vault", str(vault), "--name", "second"]).handler(
        parser.parse_args(["agents", "create", "--vault", str(vault), "--name", "second"])
    )
    capsys.readouterr()
    create_args = parser.parse_args(
        [
            "tokens",
            "create",
            "--vault",
            str(vault),
            "--client-name",
            "demo",
            "--read",
            "style",
        ]
    )
    rc = create_args.handler(create_args)
    assert rc == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "2 agents" in combined
    assert "--agent-id" in combined


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
    # Rich-rendered doctor report now lives in a table; the status
    # tokens OK/WARN/ERROR remain grep-stable per the UI module's
    # stability contract. The vault check is what we can reliably
    # assert on across CI / local runs.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "doctor report" in combined
    assert "vault" in combined
    # At least one of the three status tokens must appear.
    assert any(token in combined for token in ("OK", "WARN", "ERROR"))
