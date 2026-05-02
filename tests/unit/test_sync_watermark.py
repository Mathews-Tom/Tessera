"""V0.5-P9b sync watermark persistence tests.

Round-trip + corruption + identity-stability for the
``last_restored_sequence`` watermark that lives in ``_meta``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.migration import bootstrap
from tessera.sync.watermark import (
    CorruptWatermarkError,
    WatermarkError,
    clear_watermark,
    meta_key_for,
    read_watermark,
    store_identity,
    write_watermark,
)
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, new_salt


def _open_fresh_vault(tmp_path: Path) -> tuple[Path, ProtectedKey]:
    """Bootstrap a fresh SQLCipher vault under a known key and
    return the path + ProtectedKey for caller-side opens."""

    salt = new_salt()
    key = derive_key(b"test-passphrase", salt)
    path = tmp_path / "vault.db"
    bootstrap(path, key)
    return path, key


@pytest.mark.unit
def test_store_identity_is_stable_for_same_inputs() -> None:
    a = store_identity(endpoint="https://s3.example.com", bucket="b1", prefix="p1")
    b = store_identity(endpoint="https://s3.example.com", bucket="b1", prefix="p1")
    assert a == b


@pytest.mark.unit
def test_store_identity_differs_per_bucket() -> None:
    a = store_identity(endpoint="https://s3.example.com", bucket="b1", prefix="")
    b = store_identity(endpoint="https://s3.example.com", bucket="b2", prefix="")
    assert a != b


@pytest.mark.unit
def test_store_identity_differs_per_endpoint() -> None:
    a = store_identity(endpoint="https://s3.us-east-1.example.com", bucket="b", prefix="")
    b = store_identity(endpoint="https://s3.us-west-2.example.com", bucket="b", prefix="")
    assert a != b


@pytest.mark.unit
def test_store_identity_differs_per_prefix() -> None:
    a = store_identity(endpoint="https://s3.example.com", bucket="b", prefix="vault-A")
    b = store_identity(endpoint="https://s3.example.com", bucket="b", prefix="vault-B")
    assert a != b


@pytest.mark.unit
def test_store_identity_normalizes_trailing_slash_and_case() -> None:
    """Cosmetic operator typos must not split one logical store
    into two distinct watermarks. Trailing endpoint slash, scheme
    case, and prefix wrapping slashes all collapse to the same
    store identity."""

    a = store_identity(endpoint="https://s3.example.com/", bucket="b", prefix="/p/")
    b = store_identity(endpoint="HTTPS://S3.EXAMPLE.COM", bucket="b", prefix="p")
    assert a == b


@pytest.mark.unit
def test_store_identity_is_truncated_hex() -> None:
    sid = store_identity(endpoint="https://s3.example.com", bucket="b", prefix="")
    assert len(sid) == 32
    assert all(c in "0123456789abcdef" for c in sid)


@pytest.mark.unit
def test_meta_key_carries_prefix() -> None:
    sid = "0" * 32
    assert meta_key_for(sid) == "sync_watermark_" + sid


@pytest.mark.unit
def test_read_watermark_returns_zero_when_absent(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    with VaultConnection.open(path, key) as vc:
        assert read_watermark(vc.connection, store_id="a" * 32) == 0


@pytest.mark.unit
def test_write_then_read_watermark_round_trip(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    sid = store_identity(endpoint="https://s3.example.com", bucket="b", prefix="")
    with VaultConnection.open(path, key) as vc:
        write_watermark(vc.connection, store_id=sid, sequence=7)
        assert read_watermark(vc.connection, store_id=sid) == 7
        write_watermark(vc.connection, store_id=sid, sequence=12)
        assert read_watermark(vc.connection, store_id=sid) == 12


@pytest.mark.unit
def test_two_stores_have_independent_watermarks(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    sid_a = store_identity(endpoint="https://s3.example.com", bucket="bucket-a", prefix="")
    sid_b = store_identity(endpoint="https://s3.example.com", bucket="bucket-b", prefix="")
    with VaultConnection.open(path, key) as vc:
        write_watermark(vc.connection, store_id=sid_a, sequence=5)
        write_watermark(vc.connection, store_id=sid_b, sequence=11)
        assert read_watermark(vc.connection, store_id=sid_a) == 5
        assert read_watermark(vc.connection, store_id=sid_b) == 11


@pytest.mark.unit
def test_write_watermark_rejects_zero_or_negative(tmp_path: Path) -> None:
    """Sequence 0 means 'no pull has happened yet' and is the
    implicit return of read_watermark when no row exists. Writing
    it explicitly would conflate 'never pulled' with 'pulled and
    persisted zero', so reject."""

    path, key = _open_fresh_vault(tmp_path)
    with VaultConnection.open(path, key) as vc:
        with pytest.raises(WatermarkError, match=">= 1"):
            write_watermark(vc.connection, store_id="a" * 32, sequence=0)
        with pytest.raises(WatermarkError, match=">= 1"):
            write_watermark(vc.connection, store_id="a" * 32, sequence=-1)


@pytest.mark.unit
def test_corrupt_watermark_value_raises(tmp_path: Path) -> None:
    """A non-integer stored value indicates external tampering or
    a serialization bug. Surface as CorruptWatermarkError rather
    than silently resetting to 0 (which would accept a replay of
    any previously-restored snapshot)."""

    path, key = _open_fresh_vault(tmp_path)
    sid = "a" * 32
    with VaultConnection.open(path, key) as vc:
        vc.connection.execute(
            "INSERT INTO _meta(key, value) VALUES (?, ?)",
            (meta_key_for(sid), "not-a-number"),
        )
        with pytest.raises(CorruptWatermarkError, match="not an integer"):
            read_watermark(vc.connection, store_id=sid)


@pytest.mark.unit
def test_negative_stored_watermark_raises(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    sid = "a" * 32
    with VaultConnection.open(path, key) as vc:
        vc.connection.execute(
            "INSERT INTO _meta(key, value) VALUES (?, ?)",
            (meta_key_for(sid), "-3"),
        )
        with pytest.raises(CorruptWatermarkError, match="negative"):
            read_watermark(vc.connection, store_id=sid)


@pytest.mark.unit
def test_clear_watermark_removes_row(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    sid = "a" * 32
    with VaultConnection.open(path, key) as vc:
        write_watermark(vc.connection, store_id=sid, sequence=5)
        assert read_watermark(vc.connection, store_id=sid) == 5
        clear_watermark(vc.connection, store_id=sid)
        assert read_watermark(vc.connection, store_id=sid) == 0


@pytest.mark.unit
def test_clear_watermark_no_op_when_absent(tmp_path: Path) -> None:
    path, key = _open_fresh_vault(tmp_path)
    with VaultConnection.open(path, key) as vc:
        # No write before clear — must not raise.
        clear_watermark(vc.connection, store_id="a" * 32)
        assert read_watermark(vc.connection, store_id="a" * 32) == 0
