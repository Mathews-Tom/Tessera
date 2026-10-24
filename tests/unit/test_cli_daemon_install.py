"""``tessera daemon install`` / ``uninstall`` with HOME sandbox."""

from __future__ import annotations

import configparser
import os
import plistlib
import shlex
import sys
from pathlib import Path

import keyring
import pytest

from tessera.cli import daemon_cmd
from tessera.cli.__main__ import _build_parser
from tessera.cli._common import open_vault
from tessera.daemon.config import DaemonConfig, resolve_config
from tessera.daemon.control import ControlError
from tessera.migration import bootstrap
from tessera.vault import keyring_cache
from tessera.vault.encryption import derive_key, new_salt, save_salt


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


@pytest.fixture
def cached_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Exercise the real encoding helpers without accessing a user's OS keyring."""
    entries: dict[tuple[str, str], str] = {}

    def store(service: str, username: str, password: str) -> None:
        entries[(service, username)] = password

    monkeypatch.setattr(keyring, "set_password", store)
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, username: entries.get((service, username)),
    )
    return entries


@pytest.fixture
def offline_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    monkeypatch.delenv("TESSERA_PASSPHRASE_ENV", raising=False)

    return tmp_path


@pytest.fixture
def control_path_present(offline_home: Path) -> Path:
    """Provide endpoint presence for tests mocking control status responses."""
    path = resolve_config().socket_path
    path.parent.mkdir(parents=True)
    path.touch()
    return path


@pytest.fixture
def encrypted_vault(offline_home: Path) -> tuple[Path, str]:
    path = offline_home / "My Vault" / "vault.db"
    path.parent.mkdir()
    salt = new_salt()
    with derive_key(b"startup-test-secret", salt) as key:
        bootstrap(path, key)
    save_salt(path, salt)
    with open_vault(path, bytearray(b"startup-test-secret")) as vault:
        vault_id = vault.state.vault_id
    return path, vault_id


def _service_args(home: Path, platform: str) -> tuple[list[str], str]:
    if platform == "darwin":
        body = (home / "Library/LaunchAgents/com.tessera.daemon.plist").read_text()
        plist = plistlib.loads(body.encode())
        assert "EnvironmentVariables" not in plist
        return list(plist["ProgramArguments"]), body
    body = (home / ".config/systemd/user/tesserad.service").read_text()
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(body)
    assert "Environment" not in unit["Service"]
    return shlex.split(unit["Service"]["ExecStart"]), body


@pytest.mark.unit
@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("auth_source", ["env", "custom_env", "flag"])
def test_keyring_install_caches_offline_identity_without_secret_in_service(
    offline_home: Path,
    encrypted_vault: tuple[Path, str],
    cached_keyring: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    auth_source: str,
) -> None:
    path, vault_id = encrypted_vault
    monkeypatch.setattr(sys, "platform", platform)
    argv = ["daemon", "install", "--keyring", "--vault", str(path)]
    if auth_source == "flag":
        argv.extend(["--passphrase", "startup-test-secret"])
    elif auth_source == "custom_env":
        monkeypatch.setenv("TESSERA_PASSPHRASE_ENV", "TEST_STARTUP_PASSPHRASE")
        monkeypatch.setenv("TEST_STARTUP_PASSPHRASE", "startup-test-secret")
    else:
        monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")
    args = _build_parser().parse_args(argv)
    assert args.handler(args) == 0
    assert set(cached_keyring) == {(keyring_cache.SERVICE_NAME, vault_id)}
    assert keyring_cache.load_passphrase(vault_id) == bytearray(b"startup-test-secret")
    program, body = _service_args(offline_home, platform)
    assert program[3:] == [
        "daemon",
        "start-fg",
        "--vault",
        str(path),
        "--keyring-vault-id",
        vault_id,
    ]
    output = capsys.readouterr()
    assert "startup-test-secret" not in body + output.out + output.err
    assert "--passphrase" not in program


@pytest.mark.unit
def test_keyring_install_uses_running_daemon_without_opening_vault(
    offline_home: Path,
    control_path_present: Path,
    encrypted_vault: tuple[Path, str],
    cached_keyring: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, vault_id = encrypted_vault
    monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")

    async def live(*args: object, **kwargs: object) -> dict[str, str]:
        identity = vault_id if kwargs["method"] == "verify_keyring_install" else "unverified"
        return {"vault_path": str(path), "vault_id": identity}

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("must not open a vault owned by the live daemon")

    monkeypatch.setattr(daemon_cmd, "call_control", live)
    monkeypatch.setattr(daemon_cmd, "open_vault", forbidden)
    args = _build_parser().parse_args(["daemon", "install", "--keyring", "--vault", str(path)])
    assert args.handler(args) == 0
    assert set(cached_keyring) == {(keyring_cache.SERVICE_NAME, vault_id)}
    assert keyring_cache.load_passphrase(vault_id) == bytearray(b"startup-test-secret")
    program, body = _service_args(offline_home, sys.platform)
    assert program[-2:] == ["--keyring-vault-id", vault_id]
    assert "startup-test-secret" not in body


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["status", "verify_keyring_install"])
@pytest.mark.parametrize(
    "response",
    [
        {"vault_path": "/another/vault.db", "vault_id": "other"},
        {"vault_id": "no-path"},
        {"vault_path": "", "vault_id": "no-valid-path"},
    ],
)
def test_keyring_install_rejects_unbound_live_identity(
    offline_home: Path,
    control_path_present: Path,
    encrypted_vault: tuple[Path, str],
    cached_keyring: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, str],
    stage: str,
) -> None:
    path, vault_id = encrypted_vault
    monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")

    async def live(*args: object, **kwargs: object) -> dict[str, str]:
        if kwargs["method"] == stage:
            return response
        return {"vault_path": str(path), "vault_id": vault_id}

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("must not open a vault while a daemon is running")

    monkeypatch.setattr(daemon_cmd, "call_control", live)
    monkeypatch.setattr(daemon_cmd, "open_vault", forbidden)
    args = _build_parser().parse_args(
        [
            "daemon",
            "install",
            "--keyring",
            "--vault",
            str(path),
        ]
    )
    assert args.handler(args) == 1
    assert cached_keyring == {}
    assert not (offline_home / "Library/LaunchAgents/com.tessera.daemon.plist").exists()
    assert not (offline_home / ".config/systemd/user/tesserad.service").exists()


@pytest.mark.unit
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_keyring_install_verification_failure_preserves_previous_installation(
    offline_home: Path,
    control_path_present: Path,
    encrypted_vault: tuple[Path, str],
    cached_keyring: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    path, vault_id = encrypted_vault
    monkeypatch.setattr(sys, "platform", platform)
    keyring_cache.cache_passphrase(vault_id, bytearray(b"startup-test-secret"))
    previous_cache = cached_keyring.copy()
    parser = _build_parser()
    initial = parser.parse_args(["daemon", "install", "--vault", str(path)])
    assert initial.handler(initial) == 0
    service_path = offline_home / (
        "Library/LaunchAgents/com.tessera.daemon.plist"
        if platform == "darwin"
        else ".config/systemd/user/tesserad.service"
    )
    previous_service = service_path.read_bytes()
    capsys.readouterr()

    async def live(*args: object, **kwargs: object) -> dict[str, str]:
        if kwargs["method"] == "status":
            return {"vault_path": str(path), "vault_id": vault_id}
        raise ControlError("passphrase does not unlock the running vault")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("credential rejection must not reopen the daemon-owned vault")

    monkeypatch.setattr(daemon_cmd, "call_control", live)
    monkeypatch.setattr(daemon_cmd, "open_vault", forbidden)
    candidate = bytearray(b"mistyped-startup-secret")
    monkeypatch.setattr(daemon_cmd, "resolve_passphrase", lambda explicit: candidate)
    args = parser.parse_args(["daemon", "install", "--keyring", "--vault", str(path)])
    assert args.handler(args) == 1
    assert cached_keyring == previous_cache
    assert service_path.read_bytes() == previous_service
    assert candidate == bytearray(len(b"mistyped-startup-secret"))
    output = capsys.readouterr()
    assert "passphrase" in output.out + output.err
    assert "mistyped-startup-secret" not in output.out + output.err


@pytest.mark.unit
def test_keyring_install_refuses_offline_open_with_live_pid(
    offline_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")
    config = resolve_config()
    config.pid_path.parent.mkdir(parents=True)
    config.pid_path.write_text("1234")
    monkeypatch.setattr(os, "kill", lambda pid, signal: None)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("must not open a potentially owned vault")

    monkeypatch.setattr(daemon_cmd, "open_vault", forbidden)
    args = _build_parser().parse_args(
        [
            "daemon",
            "install",
            "--keyring",
            "--vault",
            str(offline_home / "vault.db"),
        ]
    )
    assert args.handler(args) == 1


@pytest.mark.unit
@pytest.mark.parametrize("error_type", [ControlError, ConnectionError, TimeoutError])
def test_keyring_install_refuses_control_error_without_offline_open(
    offline_home: Path,
    control_path_present: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")

    async def unavailable(*args: object, **kwargs: object) -> dict[str, object]:
        raise error_type("status unavailable")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("must not open a potentially owned vault")

    monkeypatch.setattr(daemon_cmd, "call_control", unavailable)
    monkeypatch.setattr(daemon_cmd, "open_vault", forbidden)
    args = _build_parser().parse_args(
        [
            "daemon",
            "install",
            "--keyring",
            "--vault",
            str(offline_home / "vault.db"),
        ]
    )
    assert args.handler(args) == 1


@pytest.mark.unit
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_generated_keyring_service_starts_with_sanitized_env(
    offline_home: Path,
    encrypted_vault: tuple[Path, str],
    cached_keyring: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    path, vault_id = encrypted_vault
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")
    parser = _build_parser()
    args = parser.parse_args(["daemon", "install", "--keyring", "--vault", str(path)])
    assert args.handler(args) == 0
    program, _body = _service_args(offline_home, platform)
    monkeypatch.delenv("TESSERA_PASSPHRASE")
    monkeypatch.delenv("TESSERA_PASSPHRASE_ENV", raising=False)
    observed: list[DaemonConfig] = []

    async def run(config: DaemonConfig) -> None:
        assert config.passphrase is not None
        with open_vault(config.vault_path, bytearray(config.passphrase)) as vault:
            assert vault.state.vault_id == vault_id == config.expected_vault_id
        observed.append(config)

    monkeypatch.setattr(daemon_cmd, "run_daemon", run)
    foreground = parser.parse_args(program[3:])
    assert foreground.handler(foreground) == 0
    assert len(observed) == 1


@pytest.mark.unit
@pytest.mark.parametrize("keyring_state", ["missing", "locked", "unavailable"])
def test_foreground_keyring_errors_do_not_fall_back_to_env(
    offline_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    keyring_state: str,
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "must-not-be-used")

    def load(vault_id: str) -> bytearray | None:
        if keyring_state == "missing":
            return None
        raise keyring_cache.KeyringUnavailableError("backend details must-not-be-used")

    async def forbidden(config: DaemonConfig) -> None:
        pytest.fail("must not start after explicit keyring resolution fails")

    monkeypatch.setattr(daemon_cmd, "load_passphrase", load)
    monkeypatch.setattr(daemon_cmd, "run_daemon", forbidden)
    args = _build_parser().parse_args(
        [
            "daemon",
            "start-fg",
            "--vault",
            str(offline_home / "vault.db"),
            "--keyring-vault-id",
            "requested-vault",
        ]
    )
    assert args.handler(args) == 1
    output = capsys.readouterr()
    assert "must-not-be-used" not in output.out + output.err
    assert "keyring" in (output.out + output.err).lower() or keyring_state == "missing"


@pytest.mark.unit
@pytest.mark.parametrize("auth_source", ["env", "flag"])
def test_foreground_preserves_non_keyring_authentication(
    offline_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_source: str,
) -> None:
    argv = ["daemon", "start-fg", "--vault", str(offline_home / "vault.db")]
    if auth_source == "env":
        monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")
    else:
        argv.extend(["--passphrase", "startup-test-secret"])
    observed: list[DaemonConfig] = []

    async def run(config: DaemonConfig) -> None:
        observed.append(config)

    def forbidden(vault_id: str) -> bytearray | None:
        pytest.fail("legacy startup must not access keyring")

    monkeypatch.setattr(daemon_cmd, "load_passphrase", forbidden)
    monkeypatch.setattr(daemon_cmd, "run_daemon", run)
    args = _build_parser().parse_args(argv)
    assert args.handler(args) == 0
    assert len(observed) == 1
    assert observed[0].passphrase == b"startup-test-secret"
    assert observed[0].expected_vault_id is None


@pytest.mark.unit
def test_default_install_never_caches_passphrase(
    offline_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "must-not-be-cached")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("caching requires explicit --keyring consent")

    monkeypatch.setattr(daemon_cmd, "cache_passphrase", forbidden)
    monkeypatch.setattr(daemon_cmd, "open_vault", forbidden)
    args = _build_parser().parse_args(
        [
            "daemon",
            "install",
            "--vault",
            str(offline_home / "vault.db"),
        ]
    )
    assert args.handler(args) == 0


@pytest.mark.unit
def test_keyring_install_failure_leaves_service_uninstalled(
    offline_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_path_present: Path,
) -> None:
    path = offline_home / "vault.db"
    monkeypatch.setenv("TESSERA_PASSPHRASE", "startup-test-secret")

    async def live(*args: object, **kwargs: object) -> dict[str, str]:
        return {"vault_path": str(path), "vault_id": "live-vault"}

    def locked(vault_id: str, passphrase: bytearray) -> None:
        raise keyring_cache.KeyringUnavailableError("backend startup-test-secret")

    monkeypatch.setattr(daemon_cmd, "call_control", live)
    monkeypatch.setattr(daemon_cmd, "cache_passphrase", locked)
    args = _build_parser().parse_args(["daemon", "install", "--keyring", "--vault", str(path)])
    assert args.handler(args) == 1
    assert not (offline_home / "Library/LaunchAgents/com.tessera.daemon.plist").exists()
    assert not (offline_home / ".config/systemd/user/tesserad.service").exists()
    output = capsys.readouterr()
    assert "startup-test-secret" not in output.out + output.err


@pytest.mark.unit
def test_foreground_keyring_does_not_log_failed_unlock_details(
    offline_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        daemon_cmd,
        "load_passphrase",
        lambda vault_id: bytearray(b"startup-test-secret"),
    )

    async def failed(config: DaemonConfig) -> None:
        raise RuntimeError("backend accidentally contains startup-test-secret")

    monkeypatch.setattr(daemon_cmd, "run_daemon", failed)
    args = _build_parser().parse_args(
        [
            "daemon",
            "start-fg",
            "--vault",
            str(offline_home / "vault.db"),
            "--keyring-vault-id",
            "requested-vault",
        ]
    )
    assert args.handler(args) == 1
    output = capsys.readouterr()
    assert "startup-test-secret" not in output.out + output.err
