"""Auto-start unit-file generation: launchd (macOS), systemd (Linux).

Emits text blobs, not filesystem writes — the CLI owns the install
path (``~/Library/LaunchAgents/com.tessera.daemon.plist`` on macOS,
``~/.config/systemd/user/tesserad.service`` on Linux) so this module
stays pure and unit-testable.

The generated units always invoke ``python -m tessera.cli daemon
start-fg`` (foreground) rather than ``start`` so launchd/systemd can
track the child process directly; daemonising under a supervisor
that already supervises is an antipattern that breaks restart logic.
"""

from __future__ import annotations

import shlex
from pathlib import Path

_LAUNCHD_LABEL = "com.tessera.daemon"
_SYSTEMD_UNIT_NAME = "tesserad.service"


def launchd_plist(
    *,
    python_executable: Path,
    vault_path: Path,
    log_path: Path,
    passphrase_env_var: str = "TESSERA_PASSPHRASE",
    keyring_vault_id: str | None = None,
) -> str:
    """Generate a launchd plist that runs tesserad as a user agent."""

    program_args = [
        str(python_executable),
        "-m",
        "tessera.cli",
        "daemon",
        "start-fg",
        "--vault",
        str(vault_path),
    ]
    if keyring_vault_id is not None:
        program_args.extend(["--keyring-vault-id", keyring_vault_id])
    program_items = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in program_args)
    environment = ""
    if keyring_vault_id is None:
        environment = f"""    <key>EnvironmentVariables</key>
    <dict>
        <key>TESSERA_PASSPHRASE_ENV</key>
        <string>{_xml_escape(passphrase_env_var)}</string>
    </dict>
"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_items}
    </array>
{environment}    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(log_path))}</string>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""


def systemd_unit(
    *,
    python_executable: Path,
    vault_path: Path,
    passphrase_env_var: str = "TESSERA_PASSPHRASE",
    keyring_vault_id: str | None = None,
) -> str:
    """Generate a systemd user unit."""

    program_args = [
        str(python_executable),
        "-m",
        "tessera.cli",
        "daemon",
        "start-fg",
        "--vault",
        str(vault_path),
    ]
    if keyring_vault_id is not None:
        program_args.extend(["--keyring-vault-id", keyring_vault_id])
    exec_start = " ".join(shlex.quote(a) for a in program_args)
    environment = (
        f"Environment=TESSERA_PASSPHRASE_ENV={passphrase_env_var}\n"
        if keyring_vault_id is None
        else ""
    )
    return f"""[Unit]
Description=Tessera substrate-independent identity daemon
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=3
{environment}
[Install]
WantedBy=default.target
"""


def launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def systemd_unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
        .replace('"', "&quot;")
    )


__all__ = [
    "launchd_plist",
    "launchd_plist_path",
    "systemd_unit",
    "systemd_unit_path",
]
