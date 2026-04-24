"""``tessera daemon {start, start-fg, stop, status, wait, install, uninstall}``."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from tessera.cli._common import CliError, fail, resolve_passphrase
from tessera.cli._ui import EMOJI, console, info, kv_panel, status, success
from tessera.daemon.config import DaemonConfig, resolve_config
from tessera.daemon.control import ControlError, call_control
from tessera.daemon.supervisor import run_daemon
from tessera.daemon.units import (
    launchd_plist,
    launchd_plist_path,
    systemd_unit,
    systemd_unit_path,
)

_READY_POLL_INTERVAL = 0.5
_DEFAULT_START_TIMEOUT = 30.0
_DEFAULT_WAIT_TIMEOUT = 60.0


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("daemon", help="daemon lifecycle")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    start = sub.add_parser(
        "start",
        help="spawn tesserad in the background and wait for ready",
    )
    start.add_argument("--vault", type=Path, required=True)
    start.add_argument("--passphrase", default=None)
    start.add_argument("--port", type=int, default=None)
    start.add_argument("--host", default=None)
    start.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_START_TIMEOUT,
        help=f"seconds to wait for readiness (default {_DEFAULT_START_TIMEOUT:.0f})",
    )
    start.set_defaults(handler=_cmd_start)

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

    wait = sub.add_parser("wait", help="block until the daemon is ready; show spinner")
    wait.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_WAIT_TIMEOUT,
        help=f"seconds to wait before giving up (default {_DEFAULT_WAIT_TIMEOUT:.0f})",
    )
    wait.set_defaults(handler=_cmd_wait)

    install = sub.add_parser("install", help="write launchd (macOS) or systemd (Linux) user unit")
    install.add_argument("--vault", type=Path, required=True)
    install.set_defaults(handler=_cmd_install)

    uninstall = sub.add_parser("uninstall", help="remove the installed unit")
    uninstall.set_defaults(handler=_cmd_uninstall)


def _cmd_start(args: argparse.Namespace) -> int:
    """Spawn the daemon detached, poll readiness with an infinite spinner.

    Fails loud if the daemon is already running (the socket's status
    call succeeds before the poll even starts), if the subprocess
    exits during the wait (stderr tail is surfaced), or if the wait
    times out (the caller can re-invoke with a larger --timeout).
    """

    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))

    config = resolve_config(
        vault_path=args.vault,
        http_host=args.host,
        http_port=args.port,
    )

    # If a daemon is already running against this vault, short-circuit
    # instead of starting a second one that would fight for the socket.
    if _daemon_already_running(config.socket_path):
        info("daemon already running (control socket responded)", emoji=EMOJI["daemon_up"])
        return 0

    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    config.pid_path.parent.mkdir(parents=True, exist_ok=True)

    # Route the daemon's stderr into the configured log file. Its
    # stdout is already directed there too by the supervisor's logging
    # convention; piping stdin from /dev/null prevents the detached
    # child from inheriting the terminal's stdin.
    log_fh = config.log_path.open("a", buffering=1)
    # The spawned subprocess inherits TESSERA_PASSPHRASE via the env so
    # --passphrase does not appear in the child's argv (visible in
    # `ps`). This matches the pattern the launchd/systemd units use.
    env = os.environ.copy()
    env["TESSERA_PASSPHRASE"] = bytes(passphrase).decode("utf-8")
    argv = [sys.executable, "-m", "tessera.cli", "daemon", "start-fg", "--vault", str(args.vault)]
    if args.port is not None:
        argv.extend(["--port", str(args.port)])
    if args.host is not None:
        argv.extend(["--host", args.host])

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        log_fh.close()
        return fail(f"failed to spawn daemon: {exc}")

    try:
        ok = _wait_until_ready(
            config=config,
            timeout_s=args.timeout,
            proc=proc,
            spinner_label="waiting for daemon to be ready",
        )
    finally:
        # The log handle is held by the child via dup() — closing ours
        # here does not close the child's, so rotation + append work.
        log_fh.close()

    if not ok:
        # _wait_until_ready already printed the reason.
        return 1

    # Query one more time for the panel payload so the caller sees the
    # same fields as `tessera daemon status`.
    try:
        response = asyncio.run(call_control(config.socket_path, method="status"))
    except (ConnectionError, ControlError):
        response = {}
    success(
        f"daemon running (pid={proc.pid}, log={config.log_path})",
        emoji=EMOJI["daemon_up"],
    )
    kv_panel(
        "daemon",
        {
            "pid": str(proc.pid),
            "vault_id": str(response.get("vault_id", "?")),
            "schema_version": str(response.get("schema_version", "?")),
            "active_model_id": str(response.get("active_model_id", "?")),
            "log_path": str(config.log_path),
        },
        emoji=EMOJI["daemon_up"],
    )
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    """Block until the daemon's control socket answers, with a spinner.

    Useful when the daemon was started by something else (launchd,
    systemd, a `start-fg &` invocation) and the caller wants to
    synchronise with its readiness before issuing the next command.
    """

    config = resolve_config()
    if _wait_until_ready(
        config=config,
        timeout_s=args.timeout,
        proc=None,
        spinner_label="waiting for daemon to be ready",
    ):
        success("daemon ready", emoji=EMOJI["daemon_up"])
        return 0
    return 1


def _daemon_already_running(socket_path: Path) -> bool:
    """Return True when a control-socket status call succeeds right now."""

    try:
        asyncio.run(call_control(socket_path, method="status"))
    except (ConnectionError, ControlError):
        return False
    return True


def _wait_until_ready(
    *,
    config: DaemonConfig,
    timeout_s: float,
    proc: subprocess.Popen[bytes] | None,
    spinner_label: str,
) -> bool:
    """Poll the control socket with an infinite spinner until ready.

    Returns True on success. Prints a failure line and returns False
    on subprocess death (when ``proc`` is supplied) or on timeout.
    """

    deadline = time.monotonic() + timeout_s
    with status(spinner_label, emoji=EMOJI["daemon_up"]):
        while True:
            if proc is not None and proc.poll() is not None:
                # Child died during the wait. Surface the tail so the
                # operator knows why.
                tail = _tail_log(config.log_path, lines=20)
                console.print(
                    f"[tessera.error]✗ ERROR[/] daemon exited before ready (rc={proc.returncode})"
                )
                if tail:
                    console.print(tail)
                return False
            try:
                asyncio.run(call_control(config.socket_path, method="status"))
                return True
            except (ConnectionError, ControlError):
                pass
            if time.monotonic() >= deadline:
                console.print(
                    f"[tessera.error]✗ ERROR[/] timed out after {timeout_s:.0f}s "
                    f"waiting for daemon (socket: {config.socket_path})"
                )
                return False
            time.sleep(_READY_POLL_INTERVAL)


def _tail_log(path: Path, *, lines: int) -> str:
    """Return the last ``lines`` from ``path`` or an empty string on failure.

    Best-effort: an inaccessible log is a diagnostic inconvenience, not
    a hard error the calling handler should propagate.
    """

    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # Read up to 8 KiB backwards — enough for ~20 lines of audit.
            read_back = min(size, 8 * 1024)
            fh.seek(size - read_back)
            raw = fh.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


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
    info(f"starting daemon (foreground) against {args.vault}", emoji=EMOJI["daemon_up"])
    try:
        asyncio.run(run_daemon(config))
    except Exception as exc:
        return fail(f"daemon failed: {type(exc).__name__}: {exc}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    """Stop a running daemon. Idempotent — a stopped daemon returns exit 0.

    Matches systemctl/launchctl conventions: calling ``stop`` against an
    already-stopped unit is a success, not an error. Without this
    tolerance, idempotent teardown scripts (``scripts/demo_reset.sh``,
    pre-recording cleanup steps) would have to shell-quote the error or
    prefix ``|| true`` everywhere.
    """

    del args
    config = resolve_config()
    # Fast path: if the socket doesn't even exist, the daemon isn't
    # running. Skip the control-plane dial entirely and report the
    # desired state as achieved.
    if not config.socket_path.exists():
        info("daemon already stopped (no control socket)", emoji=EMOJI["daemon_down"])
        return 0

    with status("sending stop signal", emoji=EMOJI["daemon_down"]):
        try:
            asyncio.run(call_control(config.socket_path, method="stop"))
        except OSError:
            # Every failure mode below means "daemon not reachable, so
            # it's already stopped":
            #   - FileNotFoundError (ENOENT): socket file gone
            #   - ConnectionRefusedError (ECONNREFUSED): stale socket
            #   - OSError with ENOTSOCK: a non-socket file sitting at
            #     the path (leftover from an earlier crash of a
            #     different kind)
            # All three are subclasses of OSError, so one except
            # clause covers the "stop against already-stopped" case.
            # Clean up any stale file sitting at the socket path so a
            # follow-up ``daemon start`` does not trip on it.
            config.socket_path.unlink(missing_ok=True)
            info("daemon already stopped", emoji=EMOJI["daemon_down"])
            return 0
        except ControlError as exc:
            return fail(f"stop failed: {exc}")
    success("stop signal sent", emoji=EMOJI["daemon_down"])
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    del args
    config = resolve_config()
    # Same tolerance as _cmd_stop: a missing / non-listening / stale
    # socket means the daemon is not running. Return a clear
    # "not running" line with exit 1 so shell scripts can branch on
    # `if tessera daemon status; then ...; fi`, which is the
    # conventional contract for `status` subcommands.
    if not config.socket_path.exists():
        return fail("daemon not running (no control socket)")

    with status("querying daemon", emoji=EMOJI["daemon_up"]):
        try:
            response = asyncio.run(call_control(config.socket_path, method="status"))
        except OSError:
            return fail("daemon not running (control socket unreachable)")
        except ControlError as exc:
            return fail(f"status failed: {exc}")
    kv_panel(
        "daemon status",
        {
            key: str(response.get(key, "?"))
            for key in ("vault_id", "vault_path", "schema_version", "active_model_id")
        },
        emoji=EMOJI["daemon_up"],
    )
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
        success(f"wrote {path}", emoji=EMOJI["daemon_up"])
        info(f"load via: launchctl bootstrap gui/$(id -u) {path}")
        return 0
    path = systemd_unit_path(home)
    body = systemd_unit(python_executable=python_exe, vault_path=args.vault)
    _write_unit(path, body)
    success(f"wrote {path}", emoji=EMOJI["daemon_up"])
    info(
        "enable via: systemctl --user daemon-reload && systemctl --user enable --now tesserad",
    )
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    del args
    home = Path(os.path.expanduser("~"))
    removed_any = False
    for path in (launchd_plist_path(home), systemd_unit_path(home)):
        if path.exists():
            path.unlink()
            success(f"removed {path}", emoji=EMOJI["forget"])
            removed_any = True
    if not removed_any:
        info("no installed units found")
    return 0


def _write_unit(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, 0o644)
