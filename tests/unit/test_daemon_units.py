"""launchd / systemd unit text generation."""

from __future__ import annotations

import configparser
import plistlib
import shlex
from pathlib import Path

import pytest

from tessera.daemon.units import (
    launchd_plist,
    launchd_plist_path,
    systemd_unit,
    systemd_unit_path,
)


@pytest.mark.unit
@pytest.mark.parametrize("vault_id", [None, "vault-identity"])
def test_launchd_plist_service_options(vault_id: str | None) -> None:
    vault_path = Path("/Users/x/My Vault/a&b<c>.db")
    log_path = Path("/tmp/a&b/log")
    body = launchd_plist(
        python_executable=Path("/usr/bin/python3"),
        vault_path=vault_path,
        log_path=log_path,
        keyring_vault_id=vault_id,
    )
    unit = plistlib.loads(body.encode())
    expected = [
        "/usr/bin/python3",
        "-m",
        "tessera.cli",
        "daemon",
        "start-fg",
        "--vault",
        str(vault_path),
    ]
    if vault_id is not None:
        expected.extend(["--keyring-vault-id", vault_id])
        assert "EnvironmentVariables" not in unit
    else:
        assert unit["EnvironmentVariables"] == {"TESSERA_PASSPHRASE_ENV": "TESSERA_PASSPHRASE"}
    assert unit["ProgramArguments"] == expected
    assert unit["Label"] == "com.tessera.daemon"
    assert unit["RunAtLoad"] is True
    assert unit["KeepAlive"] is True
    assert unit["ThrottleInterval"] >= 10
    assert unit["StandardOutPath"] == str(log_path)
    assert unit["StandardErrorPath"] == str(log_path)
    assert "--passphrase" not in expected


@pytest.mark.unit
@pytest.mark.parametrize("vault_id", [None, "vault-identity"])
def test_systemd_unit_service_options(vault_id: str | None) -> None:
    vault_path = Path("/home/x/My Vault/vault.db")
    body = systemd_unit(
        python_executable=Path("/usr/bin/python3"),
        vault_path=vault_path,
        keyring_vault_id=vault_id,
    )
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(body)
    expected = [
        "/usr/bin/python3",
        "-m",
        "tessera.cli",
        "daemon",
        "start-fg",
        "--vault",
        str(vault_path),
    ]
    if vault_id is not None:
        expected.extend(["--keyring-vault-id", vault_id])
        assert "Environment" not in unit["Service"]
    else:
        assert unit["Service"]["Environment"] == "TESSERA_PASSPHRASE_ENV=TESSERA_PASSPHRASE"
    assert shlex.split(unit["Service"]["ExecStart"]) == expected
    assert unit["Service"]["Restart"] == "on-failure"
    assert int(unit["Service"]["RestartSec"]) >= 3
    assert unit["Install"]["WantedBy"] == "default.target"
    assert "--passphrase" not in expected


@pytest.mark.unit
def test_paths_are_under_user_home() -> None:
    home = Path("/home/testuser")
    assert launchd_plist_path(home).is_relative_to(home)
    assert systemd_unit_path(home).is_relative_to(home)
    assert launchd_plist_path(home).name.endswith(".plist")
    assert systemd_unit_path(home).name == "tesserad.service"
