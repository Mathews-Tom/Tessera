"""OS keyring integration for optional passphrase caching.

Per docs/system-design.md §Encryption at rest §Unlock flow the passphrase may
be cached in the OS keyring on opt-in. Tessera uses the `keyring` package so
macOS (Keychain), Linux (secret-service) and Windows (Credential Manager)
share a single API. Passphrases are stored as base64url text so the keyring
transport — which expects strings — does not corrupt non-UTF-8 byte sequences
the user might legitimately choose.
"""

from __future__ import annotations

import base64
from typing import Final

import keyring
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

SERVICE_NAME: Final[str] = "tessera-vault"


class KeyringUnavailableError(Exception):
    """Raised when no usable keyring backend is configured."""


def is_available() -> bool:
    try:
        keyring.get_keyring()
    except (NoKeyringError, KeyringError):
        return False
    return True


def cache_passphrase(vault_id: str, passphrase: bytes | bytearray) -> None:
    _require_vault_id(vault_id)
    token = base64.urlsafe_b64encode(bytes(passphrase)).decode("ascii")
    try:
        keyring.set_password(SERVICE_NAME, vault_id, token)
    except (NoKeyringError, KeyringError) as exc:
        raise KeyringUnavailableError(f"keyring write failed: {exc}") from exc


def load_passphrase(vault_id: str) -> bytearray | None:
    _require_vault_id(vault_id)
    try:
        token = keyring.get_password(SERVICE_NAME, vault_id)
    except (NoKeyringError, KeyringError) as exc:
        raise KeyringUnavailableError(f"keyring read failed: {exc}") from exc
    if token is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii"))
    except ValueError as exc:
        raise KeyringUnavailableError(
            f"keyring entry for {vault_id!r} is not valid base64"
        ) from exc
    return bytearray(decoded)


def clear_passphrase(vault_id: str) -> bool:
    _require_vault_id(vault_id)
    try:
        keyring.delete_password(SERVICE_NAME, vault_id)
    except PasswordDeleteError:
        return False
    except (NoKeyringError, KeyringError) as exc:
        raise KeyringUnavailableError(f"keyring delete failed: {exc}") from exc
    return True


def _require_vault_id(vault_id: str) -> None:
    if not vault_id:
        raise ValueError("vault_id must be a non-empty string")
