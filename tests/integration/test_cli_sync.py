"""End-to-end ``tessera sync`` exercise against an in-process fake.

Covers the four V0.5-P9b CLI subcommands: setup, status, push,
pull. Uses the in-process httpx MockTransport S3 backend (the
same one ``tests.unit.test_sync_s3`` builds) and an in-memory
keyring fake so no real network or OS keyring is touched.

Pattern lifted from :mod:`tests.integration.test_cli_audit_verify`:
bootstrap a vault, drive the CLI parser via ``cli_main([...])``,
assert exit codes + side-effects.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tessera.cli import sync_cmd as sync_cli
from tessera.cli.__main__ import main as cli_main
from tessera.migration import bootstrap
from tessera.sync import config as sync_config
from tessera.sync.s3 import S3BlobStore, S3Config
from tessera.sync.watermark import meta_key_for, store_identity
from tessera.vault import keyring_cache
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

# Reuse the canonical fake S3 backend.
from tests.unit.test_sync_s3 import _FakeS3Backend

_PASSPHRASE = b"correct horse battery staple for cli sync"
_BUCKET = "tessera-cli-test"
_ENDPOINT = "https://s3.us-east-1.amazonaws.com"
_REGION = "us-east-1"
_ACCESS_KEY = "AKIDEXAMPLE"
_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def _bootstrap_vault(path: Path) -> None:
    """Create a fresh vault and persist the salt sidecar in the
    location ``load_salt`` expects (next to the vault file)."""

    from tessera.vault.encryption import save_salt

    salt = new_salt()
    save_salt(path, salt)
    k = derive_key(bytearray(_PASSPHRASE), salt)
    bootstrap(path, k)
    k.wipe()


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Replace ``tessera.vault.keyring_cache`` calls with an
    in-memory dict so tests don't touch the OS keyring."""

    storage: dict[tuple[str, str], str] = {}

    def _store_password(service: str, username: str, value: str) -> None:
        storage[(service, username)] = value

    def _load_password(service: str, username: str) -> str | None:
        return storage.get((service, username))

    def _clear_password(service: str, username: str) -> bool:
        return storage.pop((service, username), None) is not None

    monkeypatch.setattr(keyring_cache, "store_password", _store_password)
    monkeypatch.setattr(keyring_cache, "load_password", _load_password)
    monkeypatch.setattr(keyring_cache, "clear_password", _clear_password)
    return storage


@pytest.fixture
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> _FakeS3Backend:
    """Replace S3BlobStore construction in the CLI module so every
    instance routes through an in-process httpx MockTransport."""

    backend = _FakeS3Backend()
    backend.add_bucket(_BUCKET)

    def _factory(config: S3Config, **kwargs: object) -> S3BlobStore:
        transport = httpx.MockTransport(backend.handler())
        return S3BlobStore(config, transport=transport)

    monkeypatch.setattr(sync_cli, "S3BlobStore", _factory)
    return backend


@pytest.mark.integration
def test_setup_status_round_trip(
    tmp_path: Path,
    fake_keyring: dict[tuple[str, str], str],
    fake_s3: _FakeS3Backend,
) -> None:
    """End-to-end CLI flow: setup persists config + creds; status
    reads them back and reports a reachable bucket with no
    manifests yet."""

    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)

    rc = cli_main(
        [
            "sync",
            "setup",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
            "--endpoint",
            _ENDPOINT,
            "--bucket",
            _BUCKET,
            "--region",
            _REGION,
            "--access-key",
            _ACCESS_KEY,
            "--secret-key",
            _SECRET_KEY,
        ]
    )
    assert rc == 0

    # Config landed in _meta.
    salt = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    key = derive_key(bytearray(_PASSPHRASE), salt)
    with VaultConnection.open(vault_path, key) as vc:
        stored = sync_config.load_config(vc.connection)
    assert stored.endpoint == _ENDPOINT
    assert stored.bucket == _BUCKET

    # Credentials landed in the (faked) keyring.
    sid = store_identity(endpoint=_ENDPOINT, bucket=_BUCKET, prefix="")
    service = f"tessera-sync-{sid}"
    assert fake_keyring[(service, "access_key_id")] == _ACCESS_KEY
    assert fake_keyring[(service, "secret_access_key")] == _SECRET_KEY

    # Status round-trips against the fake S3 backend.
    rc = cli_main(
        [
            "sync",
            "status",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0


@pytest.mark.integration
def test_status_before_setup_returns_two(
    tmp_path: Path,
    fake_keyring: dict[tuple[str, str], str],
    fake_s3: _FakeS3Backend,
) -> None:
    """Running ``status`` before ``setup`` must surface the
    SyncNotConfiguredError as a clear "run setup first" message
    and exit 2 (config error)."""

    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)

    rc = cli_main(
        [
            "sync",
            "status",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 2


@pytest.mark.integration
def test_push_pull_round_trip(
    tmp_path: Path,
    fake_keyring: dict[tuple[str, str], str],
    fake_s3: _FakeS3Backend,
) -> None:
    """Setup → push → pull → watermark advanced. The full v0.5
    exit-gate flow expressed as CLI invocations."""

    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)

    cli_main(
        [
            "sync",
            "setup",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
            "--endpoint",
            _ENDPOINT,
            "--bucket",
            _BUCKET,
            "--region",
            _REGION,
            "--access-key",
            _ACCESS_KEY,
            "--secret-key",
            _SECRET_KEY,
        ]
    )

    rc = cli_main(
        [
            "sync",
            "push",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0

    # The push wrote a manifest to the fake backend.
    bucket = fake_s3.buckets[_BUCKET]
    assert "manifests/1.json" in bucket
    blob_keys = [k for k in bucket if k.startswith("blobs/")]
    assert len(blob_keys) == 1

    # Pull restores the same vault. A watermark gets persisted.
    target = tmp_path / "restored.db"
    target_salt = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    (target.parent / (target.name + ".salt")).write_bytes(target_salt)

    rc = cli_main(
        [
            "sync",
            "pull",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
            "--target",
            str(target),
        ]
    )
    assert rc == 0
    assert target.exists()

    # Pull-without-target advances the watermark.
    rc = cli_main(
        [
            "sync",
            "pull",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0

    salt = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    key = derive_key(bytearray(_PASSPHRASE), salt)
    sid = store_identity(endpoint=_ENDPOINT, bucket=_BUCKET, prefix="")
    with VaultConnection.open(vault_path, key) as vc:
        row = vc.connection.execute(
            "SELECT value FROM _meta WHERE key = ?", (meta_key_for(sid),)
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1


@pytest.mark.integration
def test_push_fails_clearly_when_credentials_missing(
    tmp_path: Path,
    fake_keyring: dict[tuple[str, str], str],
    fake_s3: _FakeS3Backend,
) -> None:
    """If ``setup`` ran but the keyring entries were later
    cleared (e.g. operator changed OS user), push must exit
    nonzero with a clear message rather than silently succeeding
    with empty creds."""

    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)
    cli_main(
        [
            "sync",
            "setup",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
            "--endpoint",
            _ENDPOINT,
            "--bucket",
            _BUCKET,
            "--region",
            _REGION,
            "--access-key",
            _ACCESS_KEY,
            "--secret-key",
            _SECRET_KEY,
        ]
    )
    fake_keyring.clear()

    rc = cli_main(
        [
            "sync",
            "push",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc != 0


@pytest.mark.integration
def test_sync_command_with_no_subcommand_prints_help(tmp_path: Path) -> None:
    rc = cli_main(["sync"])
    assert rc == 2
