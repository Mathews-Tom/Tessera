"""Envelope encryption for BYO sync blobs (V0.5-P9).

Each push produces a fresh 32-byte data-encryption key (DEK)
sampled from the OS CSPRNG. The DEK encrypts the vault blob with
AES-256-GCM (authenticated; the GCM tag detects any byte flip on
pull). The DEK is then wrapped by the master key — also
AES-256-GCM, with a separate random nonce — and the wrapped DEK
travels in the manifest.

Why fresh DEK per push: a leaked DEK only compromises one
snapshot; the next push generates a new key. Rotating the master
key (e.g., after a vault passphrase change) does not require
re-encrypting every historical snapshot — only re-wrapping the
DEKs.

The master key is the SQLCipher key derived from the user
passphrase via argon2id (see ``vault/encryption.py``). Reusing it
for envelope wrap is a design choice: a separate sync-only key
would force a separate keyring entry, complicate the user setup,
and provide no security gain since both keys live behind the
same passphrase. The threat model (S6) treats them as the same
trust boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 key length. The master key derived by argon2id is 32
# bytes (see ``KDF_V1.hash_len``); the DEK matches so wrap and
# data encryption use the same primitive.
KEY_BYTES: Final[int] = 32

# AES-GCM nonce length per NIST SP 800-38D recommendation. 96-bit
# random nonces are safe for ~2^32 messages under the same key;
# we generate a fresh DEK per push so the practical limit is one.
NONCE_BYTES: Final[int] = 12


class EnvelopeError(Exception):
    """Base class for envelope-encryption failures."""


class InvalidCiphertextError(EnvelopeError):
    """Authentication tag did not verify on decrypt — tampering or wrong key."""


class InvalidKeyLengthError(EnvelopeError):
    """Caller supplied a key of the wrong length for AES-256-GCM."""


@dataclass(frozen=True, slots=True)
class WrappedKey:
    """A DEK sealed under the master key.

    ``ciphertext`` already includes the AES-GCM tag at the end (the
    cryptography library appends it). ``nonce`` is the random
    96-bit nonce used for the wrap operation. Both fields ride
    inside the manifest as base64-encoded blobs.
    """

    nonce: bytes
    ciphertext: bytes


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """The encrypted vault payload that lives in the BlobStore.

    ``ciphertext`` is the AES-GCM output (data + tag) keyed by the
    DEK. ``nonce`` is the random 96-bit nonce used for the data
    encryption operation. The DEK itself is *not* part of this
    structure — it travels wrapped inside the manifest, under the
    master key.
    """

    nonce: bytes
    ciphertext: bytes


def generate_dek() -> bytes:
    """Sample a fresh 32-byte data-encryption key from the OS CSPRNG."""

    return os.urandom(KEY_BYTES)


def wrap_dek(*, master_key: bytes, dek: bytes) -> WrappedKey:
    """Seal ``dek`` under ``master_key`` with AES-256-GCM.

    Each wrap uses a fresh random nonce so the same DEK wrapped
    twice (which the push path never does, but a buggy caller
    might) never reuses a (key, nonce) pair — the catastrophic
    failure mode for AES-GCM.
    """

    _check_key(master_key, "master_key")
    _check_key(dek, "dek")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(master_key).encrypt(nonce, dek, associated_data=None)
    return WrappedKey(nonce=nonce, ciphertext=ciphertext)


def unwrap_dek(*, master_key: bytes, wrapped: WrappedKey) -> bytes:
    """Recover the DEK from a wrapped key.

    Raises :class:`InvalidCiphertextError` when the master key is
    wrong or the wrapped blob has been tampered with — same surface
    on both because they are indistinguishable to the receiver.
    """

    _check_key(master_key, "master_key")
    _check_nonce(wrapped.nonce)
    try:
        return AESGCM(master_key).decrypt(wrapped.nonce, wrapped.ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise InvalidCiphertextError(
            "wrapped DEK failed authentication (wrong master key or tampered manifest)"
        ) from exc


def encrypt_blob(*, dek: bytes, plaintext: bytes) -> EncryptedBlob:
    """Encrypt the vault payload under the DEK with AES-256-GCM.

    The 96-bit GCM tag is appended to the ciphertext by the
    underlying library; pulling it back through :func:`decrypt_blob`
    fails on any byte flip across the entire blob — that is the
    single-tag-over-the-file property the V0.5-P9 envelope adds on
    top of SQLCipher's per-page authentication.
    """

    _check_key(dek, "dek")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, associated_data=None)
    return EncryptedBlob(nonce=nonce, ciphertext=ciphertext)


def decrypt_blob(*, dek: bytes, blob: EncryptedBlob) -> bytes:
    """Recover the vault payload from an encrypted blob.

    Raises :class:`InvalidCiphertextError` when the DEK is wrong or
    the blob has been tampered with on the wire. The MCP /
    pull-side caller surfaces this as an aborted restore so the
    user does not silently install a corrupted vault.
    """

    _check_key(dek, "dek")
    _check_nonce(blob.nonce)
    try:
        return AESGCM(dek).decrypt(blob.nonce, blob.ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise InvalidCiphertextError(
            "encrypted blob failed authentication (wrong DEK or tampered ciphertext)"
        ) from exc


def _check_key(key: bytes, label: str) -> None:
    if not isinstance(key, bytes | bytearray):
        raise InvalidKeyLengthError(f"{label} must be bytes, got {type(key).__name__}")
    if len(key) != KEY_BYTES:
        raise InvalidKeyLengthError(
            f"{label} length {len(key)} != expected {KEY_BYTES} bytes for AES-256"
        )


def _check_nonce(nonce: bytes) -> None:
    if not isinstance(nonce, bytes | bytearray):
        raise InvalidCiphertextError(f"nonce must be bytes, got {type(nonce).__name__}")
    if len(nonce) != NONCE_BYTES:
        raise InvalidCiphertextError(
            f"nonce length {len(nonce)} != expected {NONCE_BYTES} bytes for AES-GCM"
        )


__all__ = [
    "KEY_BYTES",
    "NONCE_BYTES",
    "EncryptedBlob",
    "EnvelopeError",
    "InvalidCiphertextError",
    "InvalidKeyLengthError",
    "WrappedKey",
    "decrypt_blob",
    "encrypt_blob",
    "generate_dek",
    "unwrap_dek",
    "wrap_dek",
]
