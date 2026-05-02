"""V0.5-P9b sync config + credential persistence tests.

Direct unit coverage for ``tessera.sync.config``: round-trip,
partial-state, error class boundaries. Closes the gap
``pr-test-analyzer`` flagged at 9/10 — config.py was previously
exercised only transitively via ``test_cli_sync.py``, leaving
the half-credential leakage path and the ``clear_*`` reset paths
without dedicated tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.migration import bootstrap
from tessera.sync.config import (
    StoredConfig,
    SyncCredentialsMissingError,
    SyncNotConfiguredError,
    assemble_s3_config,
    clear_config,
    clear_credentials,
    keyring_service_for,
    load_config,
    load_credentials,
    save_config,
    save_credentials,
)
from tessera.sync.watermark import store_identity
from tessera.vault import keyring_cache
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, new_salt


def _open_fresh_vault(tmp_path: Path) -> tuple[Path, ProtectedKey]:
    salt = new_salt()
    key = derive_key(b"test-passphrase", salt)
    path = tmp_path / "vault.db"
    bootstrap(path, key)
    return path, key


def _stored() -> StoredConfig:
    return StoredConfig(
        endpoint="https://s3.us-east-1.amazonaws.com",
        bucket="b",
        region="us-east-1",
        prefix="",
    )


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    storage: dict[tuple[str, str], str] = {}

    def _store(service: str, username: str, value: str) -> None:
        storage[(service, username)] = value

    def _load(service: str, username: str) -> str | None:
        return storage.get((service, username))

    def _clear(service: str, username: str) -> bool:
        return storage.pop((service, username), None) is not None

    monkeypatch.setattr(keyring_cache, "store_password", _store)
    monkeypatch.setattr(keyring_cache, "load_password", _load)
    monkeypatch.setattr(keyring_cache, "clear_password", _clear)
    return storage


@pytest.mark.unit
def test_save_and_load_config_round_trip(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    stored = _stored()
    with VaultConnection.open(path, key) as vc:
        save_config(vc.connection, config=stored)
        loaded = load_config(vc.connection)
    assert loaded == stored


@pytest.mark.unit
def test_load_config_with_no_meta_rows_raises(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    with (
        VaultConnection.open(path, key) as vc,
        pytest.raises(SyncNotConfiguredError, match="missing _meta keys"),
    ):
        load_config(vc.connection)


@pytest.mark.unit
def test_load_config_with_partial_meta_rows_lists_missing(tmp_path: Path) -> None:
    """Partial config (e.g., bucket missing) must surface the
    specific missing keys, not a generic "not configured" message.
    Operator needs to know which row to set."""

    path, key = _open_fresh_vault(tmp_path)
    with VaultConnection.open(path, key) as vc:
        vc.connection.execute(
            "INSERT INTO _meta(key, value) VALUES ('sync_endpoint', 'https://x.com')"
        )
        with pytest.raises(SyncNotConfiguredError) as exc_info:
            load_config(vc.connection)
        message = str(exc_info.value)
        assert "bucket" in message
        assert "region" in message


@pytest.mark.unit
def test_clear_config_removes_meta_rows(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    stored = _stored()
    with VaultConnection.open(path, key) as vc:
        save_config(vc.connection, config=stored)
        clear_config(vc.connection)
        with pytest.raises(SyncNotConfiguredError):
            load_config(vc.connection)


@pytest.mark.unit
def test_clear_config_no_op_when_absent(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    with VaultConnection.open(path, key) as vc:
        # Calling clear before save must not raise.
        clear_config(vc.connection)


@pytest.mark.unit
def test_save_credentials_round_trip(fake_keyring: dict[tuple[str, str], str]) -> None:
    stored = _stored()
    save_credentials(stored=stored, access_key_id="AKID", secret_access_key="SECRET")
    access, secret = load_credentials(stored=stored)
    assert access == "AKID"
    assert secret == "SECRET"


@pytest.mark.unit
def test_load_credentials_when_both_missing_raises(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    stored = _stored()
    with pytest.raises(SyncCredentialsMissingError, match="rerun"):
        load_credentials(stored=stored)


@pytest.mark.unit
def test_load_credentials_when_only_access_key_present_raises(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    """A half-credential cannot authenticate; refuse loud rather
    than returning ``("AKID", None)`` which would let an empty
    secret reach SigV4. The credential boundary must enforce
    both-or-neither."""

    stored = _stored()
    service = keyring_service_for(stored)
    fake_keyring[(service, "access_key_id")] = "AKID"
    # secret_access_key intentionally absent.
    with pytest.raises(SyncCredentialsMissingError):
        load_credentials(stored=stored)


@pytest.mark.unit
def test_load_credentials_when_only_secret_key_present_raises(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    """Inverse of the half-credential case above. Symmetric
    coverage so a future regression that only checks one side
    of the pair fails loud."""

    stored = _stored()
    service = keyring_service_for(stored)
    fake_keyring[(service, "secret_access_key")] = "SECRET"
    # access_key_id intentionally absent.
    with pytest.raises(SyncCredentialsMissingError):
        load_credentials(stored=stored)


@pytest.mark.unit
def test_clear_credentials_removes_keyring_entries(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    stored = _stored()
    save_credentials(stored=stored, access_key_id="AKID", secret_access_key="SECRET")
    clear_credentials(stored=stored)
    with pytest.raises(SyncCredentialsMissingError):
        load_credentials(stored=stored)


@pytest.mark.unit
def test_clear_credentials_no_op_when_absent(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    """Idempotent reset: calling clear before save must not raise.
    Per the docstring contract the post-condition is "no entries
    for this store"; a no-op satisfies that."""

    stored = _stored()
    clear_credentials(stored=stored)
    # Confirm no entries were created as a side-effect.
    with pytest.raises(SyncCredentialsMissingError):
        load_credentials(stored=stored)


@pytest.mark.unit
def test_keyring_service_changes_with_store_identity() -> None:
    """Two distinct store targets must produce distinct keyring
    services so credential isolation between stores is real."""

    a = keyring_service_for(
        StoredConfig(endpoint="https://a.com", bucket="b", region="r", prefix="")
    )
    b = keyring_service_for(
        StoredConfig(endpoint="https://a.com", bucket="c", region="r", prefix="")
    )
    assert a != b


@pytest.mark.unit
def test_keyring_service_carries_store_identity_hash() -> None:
    """The keyring service string must contain the store_identity
    hash so an operator inspecting the keyring sees which store
    the entry belongs to (not just an opaque service tag)."""

    stored = StoredConfig(endpoint="https://s3.example.com", bucket="b", region="r", prefix="p")
    service = keyring_service_for(stored)
    sid = store_identity(endpoint=stored.endpoint, bucket=stored.bucket, prefix=stored.prefix)
    assert sid in service


@pytest.mark.unit
def test_assemble_s3_config_combines_stored_and_credentials() -> None:
    stored = _stored()
    s3_config = assemble_s3_config(stored=stored, access_key_id="AK", secret_access_key="SK")
    assert s3_config.endpoint == stored.endpoint
    assert s3_config.bucket == stored.bucket
    assert s3_config.region == stored.region
    assert s3_config.prefix == stored.prefix
    assert s3_config.access_key_id == "AK"
    assert s3_config.secret_access_key == "SK"


@pytest.mark.unit
def test_save_config_overwrites_prior_rows(tmp_path: Path) -> None:
    """Re-running setup against the same vault must overwrite
    rather than fail on duplicate-key. Operators reconfigure
    sync targets when buckets / regions change."""

    path, key = _open_fresh_vault(tmp_path)
    first = _stored()
    second = StoredConfig(endpoint="https://other.com", bucket="b2", region="us-west-2", prefix="p")
    with VaultConnection.open(path, key) as vc:
        save_config(vc.connection, config=first)
        save_config(vc.connection, config=second)
        loaded = load_config(vc.connection)
    assert loaded == second
