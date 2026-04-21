"""``tessera daemon install`` / ``uninstall`` with HOME sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.cli.__main__ import _build_parser


@pytest.mark.unit
def test_install_writes_launchd_or_systemd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["daemon", "install", "--vault", str(tmp_path / "v.db")])
    rc = args.handler(args)
    assert rc == 0
    out = capsys.readouterr().out
    # macOS or Linux — exactly one of the two paths must exist.
    launchd = tmp_path / "Library" / "LaunchAgents" / "com.tessera.daemon.plist"
    systemd = tmp_path / ".config" / "systemd" / "user" / "tesserad.service"
    assert launchd.exists() or systemd.exists()
    assert "wrote" in out


@pytest.mark.unit
def test_uninstall_removes_installed_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = _build_parser()
    install_args = parser.parse_args(["daemon", "install", "--vault", str(tmp_path / "v.db")])
    install_args.handler(install_args)
    capsys.readouterr()
    uninstall_args = parser.parse_args(["daemon", "uninstall"])
    rc = uninstall_args.handler(uninstall_args)
    assert rc == 0
    assert "removed" in capsys.readouterr().out


@pytest.mark.unit
def test_uninstall_without_install_is_quiet_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = _build_parser()
    args = parser.parse_args(["daemon", "uninstall"])
    assert args.handler(args) == 0
