"""Argon2id derivation, ProtectedKey lifecycle, and parameter versioning."""

from __future__ import annotations

import pytest

from tessera.vault.encryption import (
    CURRENT_KDF_VERSION,
    KDF_V1,
    KDFParams,
    ProtectedKey,
    derive_key,
    kdf_params,
    new_salt,
)


@pytest.mark.unit
def test_kdf_v1_params_match_spec() -> None:
    assert KDF_V1.version == 1
    assert KDF_V1.time_cost == 3
    assert KDF_V1.memory_cost_kib == 65536
    assert KDF_V1.parallelism == 4
    assert KDF_V1.hash_len == 32
    assert KDF_V1.salt_len == 16


@pytest.mark.unit
def test_current_kdf_version_matches_registry() -> None:
    params = kdf_params(CURRENT_KDF_VERSION)
    assert params.version == CURRENT_KDF_VERSION


@pytest.mark.unit
def test_kdf_params_unknown_version_raises() -> None:
    with pytest.raises(ValueError, match="unknown kdf version"):
        kdf_params(999)


@pytest.mark.unit
def test_new_salt_has_expected_length_and_high_entropy() -> None:
    s1 = new_salt()
    s2 = new_salt()
    assert len(s1) == KDF_V1.salt_len
    assert s1 != s2


@pytest.mark.unit
def test_derive_key_is_deterministic_for_same_inputs() -> None:
    salt = b"\x00" * KDF_V1.salt_len
    with derive_key(bytearray(b"pass"), salt) as k1, derive_key(bytearray(b"pass"), salt) as k2:
        assert k1.hex() == k2.hex()


@pytest.mark.unit
def test_derive_key_differs_across_salts() -> None:
    s1 = b"\x00" * KDF_V1.salt_len
    s2 = b"\x01" * KDF_V1.salt_len
    with derive_key(bytearray(b"pass"), s1) as k1, derive_key(bytearray(b"pass"), s2) as k2:
        assert k1.hex() != k2.hex()


@pytest.mark.unit
def test_derive_key_empty_passphrase_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        derive_key(bytearray(b""), new_salt())


@pytest.mark.unit
def test_derive_key_wrong_salt_length_rejected() -> None:
    with pytest.raises(ValueError, match="salt length"):
        derive_key(bytearray(b"pass"), b"\x00" * (KDF_V1.salt_len - 1))


@pytest.mark.unit
def test_protected_key_pragma_literal_is_quoted_blob() -> None:
    with ProtectedKey.adopt(bytes.fromhex("ff" * 32)) as key:
        lit = key.as_pragma_literal()
    assert lit.startswith("\"x'")
    assert lit.endswith("'\"")
    assert "ff" * 32 in lit


@pytest.mark.unit
def test_protected_key_wipe_is_idempotent() -> None:
    key = ProtectedKey.adopt(bytes.fromhex("ab" * 32))
    key.wipe()
    key.wipe()  # second wipe must not raise


@pytest.mark.unit
def test_protected_key_hex_after_wipe_raises() -> None:
    key = ProtectedKey.adopt(bytes.fromhex("ab" * 32))
    key.wipe()
    with pytest.raises(RuntimeError, match="wiped"):
        key.hex()


@pytest.mark.unit
def test_protected_key_length_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        ProtectedKey(0)


@pytest.mark.unit
def test_kdf_params_dataclass_is_frozen() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        KDF_V1.__setattr__("time_cost", 99)
    _ = KDFParams(version=1, time_cost=1, memory_cost_kib=1, parallelism=1, hash_len=1, salt_len=1)
