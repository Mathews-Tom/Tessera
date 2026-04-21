"""launchd / systemd unit text generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.daemon.units import (
    launchd_plist,
    launchd_plist_path,
    systemd_unit,
    systemd_unit_path,
)


@pytest.mark.unit
def test_launchd_plist_contains_required_keys() -> None:
    body = launchd_plist(
        python_executable=Path("/usr/bin/python3"),
        vault_path=Path("/Users/x/.tessera/vault.db"),
        log_path=Path("/tmp/tesserad.log"),
    )
    assert "<key>Label</key>" in body
    assert "<string>com.tessera.daemon</string>" in body
    assert "<key>ProgramArguments</key>" in body
    assert "<string>/usr/bin/python3</string>" in body
    assert "<key>RunAtLoad</key>" in body
    assert "<key>KeepAlive</key>" in body


@pytest.mark.unit
def test_launchd_plist_escapes_xml_entities() -> None:
    # A vault path with &, <, > must land as entities so plist parsers
    # do not choke on the generated file.
    body = launchd_plist(
        python_executable=Path("/usr/bin/python3"),
        vault_path=Path("/tmp/a&b<c>.db"),
        log_path=Path("/tmp/x.log"),
    )
    assert "/tmp/a&amp;b&lt;c&gt;.db" in body
    assert "a&b<c>" not in body


@pytest.mark.unit
def test_systemd_unit_contains_required_sections() -> None:
    body = systemd_unit(
        python_executable=Path("/usr/bin/python3"),
        vault_path=Path("/home/x/.tessera/vault.db"),
    )
    assert "[Unit]" in body
    assert "[Service]" in body
    assert "[Install]" in body
    assert "ExecStart=" in body
    assert "Restart=on-failure" in body
    assert "WantedBy=default.target" in body


@pytest.mark.unit
def test_paths_are_under_user_home() -> None:
    home = Path("/home/testuser")
    assert launchd_plist_path(home).is_relative_to(home)
    assert systemd_unit_path(home).is_relative_to(home)
    assert launchd_plist_path(home).name.endswith(".plist")
    assert systemd_unit_path(home).name == "tesserad.service"
