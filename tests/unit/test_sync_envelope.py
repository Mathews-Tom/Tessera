"""V0.5-P9 envelope encryption (AES-GCM blob + DEK wrap)."""

from __future__ import annotations

import os

import pytest

from tessera.sync import envelope


@pytest.mark.unit
def test_wrap_unwrap_round_trip() -> None:
    master = os.urandom(envelope.KEY_BYTES)
    dek = envelope.generate_dek()
    wrapped = envelope.wrap_dek(master_key=master, dek=dek)
    recovered = envelope.unwrap_dek(master_key=master, wrapped=wrapped)
    assert recovered == dek


@pytest.mark.unit
def test_wrap_uses_fresh_nonce_per_call() -> None:
    """Two wraps of the same DEK under the same master key must use
    different nonces. (key, nonce) reuse breaks AES-GCM
    confidentiality + authenticity catastrophically."""

    master = os.urandom(envelope.KEY_BYTES)
    dek = envelope.generate_dek()
    a = envelope.wrap_dek(master_key=master, dek=dek)
    b = envelope.wrap_dek(master_key=master, dek=dek)
    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext


@pytest.mark.unit
def test_unwrap_with_wrong_master_key_raises() -> None:
    master = os.urandom(envelope.KEY_BYTES)
    other = os.urandom(envelope.KEY_BYTES)
    dek = envelope.generate_dek()
    wrapped = envelope.wrap_dek(master_key=master, dek=dek)
    with pytest.raises(envelope.InvalidCiphertextError, match="failed authentication"):
        envelope.unwrap_dek(master_key=other, wrapped=wrapped)


@pytest.mark.unit
def test_unwrap_with_tampered_ciphertext_raises() -> None:
    master = os.urandom(envelope.KEY_BYTES)
    dek = envelope.generate_dek()
    wrapped = envelope.wrap_dek(master_key=master, dek=dek)
    flipped = bytearray(wrapped.ciphertext)
    flipped[0] ^= 0x01
    tampered = envelope.WrappedKey(nonce=wrapped.nonce, ciphertext=bytes(flipped))
    with pytest.raises(envelope.InvalidCiphertextError):
        envelope.unwrap_dek(master_key=master, wrapped=tampered)


@pytest.mark.unit
def test_blob_round_trip() -> None:
    dek = envelope.generate_dek()
    plaintext = b"the encrypted vault file" * 256
    blob = envelope.encrypt_blob(dek=dek, plaintext=plaintext)
    recovered = envelope.decrypt_blob(dek=dek, blob=blob)
    assert recovered == plaintext


@pytest.mark.unit
def test_blob_decrypt_with_wrong_dek_raises() -> None:
    dek = envelope.generate_dek()
    other = envelope.generate_dek()
    blob = envelope.encrypt_blob(dek=dek, plaintext=b"secret")
    with pytest.raises(envelope.InvalidCiphertextError):
        envelope.decrypt_blob(dek=other, blob=blob)


@pytest.mark.unit
def test_blob_decrypt_with_tampered_ciphertext_raises() -> None:
    """Single-tag-over-the-file: any byte flip in the ciphertext
    surfaces as InvalidCiphertextError. This is the core property
    the V0.5-P9 envelope adds on top of SQLCipher's per-page
    authentication."""

    dek = envelope.generate_dek()
    blob = envelope.encrypt_blob(dek=dek, plaintext=b"the vault payload" * 128)
    flipped = bytearray(blob.ciphertext)
    flipped[100] ^= 0x80
    tampered = envelope.EncryptedBlob(nonce=blob.nonce, ciphertext=bytes(flipped))
    with pytest.raises(envelope.InvalidCiphertextError):
        envelope.decrypt_blob(dek=dek, blob=tampered)


@pytest.mark.unit
def test_encrypt_uses_fresh_nonce_per_call() -> None:
    dek = envelope.generate_dek()
    a = envelope.encrypt_blob(dek=dek, plaintext=b"identical")
    b = envelope.encrypt_blob(dek=dek, plaintext=b"identical")
    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext


@pytest.mark.unit
@pytest.mark.parametrize("bad_len", [0, 1, 16, 31, 33, 64])
def test_check_key_rejects_wrong_length(bad_len: int) -> None:
    bad = b"\x00" * bad_len
    with pytest.raises(envelope.InvalidKeyLengthError, match="length"):
        envelope.wrap_dek(master_key=bad, dek=envelope.generate_dek())


@pytest.mark.unit
def test_check_key_rejects_non_bytes() -> None:
    with pytest.raises(envelope.InvalidKeyLengthError, match="must be bytes"):
        envelope.wrap_dek(master_key="not-bytes", dek=envelope.generate_dek())  # type: ignore[arg-type]


@pytest.mark.unit
def test_decrypt_rejects_wrong_nonce_length() -> None:
    dek = envelope.generate_dek()
    blob = envelope.encrypt_blob(dek=dek, plaintext=b"x")
    bad = envelope.EncryptedBlob(nonce=b"\x00" * 8, ciphertext=blob.ciphertext)
    with pytest.raises(envelope.InvalidCiphertextError, match="nonce length"):
        envelope.decrypt_blob(dek=dek, blob=bad)


@pytest.mark.unit
def test_dek_is_thirty_two_bytes() -> None:
    dek = envelope.generate_dek()
    assert len(dek) == envelope.KEY_BYTES
    assert len(dek) == 32  # AES-256
