"""``tessera daemon {start-fg, stop, status, install, uninstall}``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from tessera.cli._common import CliError, fail, resolve_passphrase
from tessera.daemon.config import resolve_config
from tessera.daemon.control import ControlError, call_control
from tessera.daemon.supervisor import run_daemon
from tessera.daemon.units import (
    launchd_plist,
    launchd_plist_path,
    systemd_unit,
    systemd_unit_path,
)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("daemon", help="daemon lifecycle")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    start_fg = sub.add_parser(
        "start-fg",
        help="run tesserad in the foreground (used by launchd/systemd)",
    )
    start_fg.add_argument("--vault", type=Path, required=True)
    start_fg.add_argument("--passphrase", default=None)
    start_fg.add_argument("--port", type=int, default=None)
    start_fg.add_argument("--host", default=None)
    start_fg.set_defaults(handler=_cmd_start_fg)

    stop = sub.add_parser("stop", help="send stop via the control socket")
    stop.set_defaults(handler=_cmd_stop)

    status = sub.add_parser("status", help="query daemon status via control socket")
    status.set_defaults(handler=_cmd_status)

    install = sub.add_parser("install", help="write launchd (macOS) or systemd (Linux) user unit")
    install.add_argument("--vault", type=Path, required=True)
    install.set_defaults(handler=_cmd_install)

    uninstall = sub.add_parser("uninstall", help="remove the installed unit")
    uninstall.set_defaults(handler=_cmd_uninstall)


def _cmd_start_fg(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    config = resolve_config(
        vault_path=args.vault,
        http_host=args.host,
        http_port=args.port,
        passphrase=bytes(passphrase),
    )
    try:
        asyncio.run(run_daemon(config))
    except Exception as exc:
        return fail(f"daemon failed: {type(exc).__name__}: {exc}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    del args
    config = resolve_config()
    try:
        asyncio.run(call_control(config.socket_path, method="stop"))
    except (ConnectionError, ControlError) as exc:
        return fail(f"stop failed: {exc}")
    print("stop signal sent")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    del args
    config = resolve_config()
    try:
        response = asyncio.run(call_control(config.socket_path, method="status"))
    except ConnectionError as exc:
        return fail(f"daemon not running (socket: {exc})")
    except ControlError as exc:
        return fail(f"status failed: {exc}")
    for key in ("vault_id", "vault_path", "schema_version", "active_model_id"):
        value = response.get(key, "?")
        print(f"{key}: {value}")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    home = Path(os.path.expanduser("~"))
    python_exe = Path(sys.executable)
    platform: str = sys.platform  # strings forces mypy off the Literal narrow
    if platform == "darwin":
        path = launchd_plist_path(home)
        body = launchd_plist(
            python_executable=python_exe,
            vault_path=args.vault,
            log_path=home / ".tessera" / "run" / "tesserad.log",
        )
        _write_unit(path, body)
        print(f"wrote {path}")
        print(f"Load via: launchctl bootstrap gui/$(id -u) {path}")
        return 0
    path = systemd_unit_path(home)
    body = systemd_unit(python_executable=python_exe, vault_path=args.vault)
    _write_unit(path, body)
    print(f"wrote {path}")
    print("Enable via: systemctl --user daemon-reload && systemctl --user enable --now tesserad")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    del args
    home = Path(os.path.expanduser("~"))
    for path in (launchd_plist_path(home), systemd_unit_path(home)):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    return 0


def _write_unit(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, 0o644)
