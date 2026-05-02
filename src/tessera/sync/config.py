"""BYO sync configuration persistence (V0.5-P9b).

Holds the non-secret S3 config (endpoint, bucket, region, prefix)
in the vault's ``_meta`` table. Secret material (access key /
secret key) lives in the OS keyring under
``tessera-sync-<store_id>`` per ADR-0022 D4.

The split between vault-stored config and keyring-stored secrets
matches the security boundary: vault is encrypted at rest under
SQLCipher (so config-disclosure on disk is gated on the master
key); secrets in the keyring are gated on the OS user-session key
and never touch the vault file. Operators rotating creds via the
keyring do not change the watermark or the store identity (per
ADR-0022 D3 the watermark is keyed by store target, not by
credential).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import sqlcipher3

from tessera.sync.s3 import S3Config
from tessera.sync.watermark import store_identity
from tessera.vault import keyring_cache

_META_PREFIX: Final[str] = "sync_"
_KEYRING_SERVICE_PREFIX: Final[str] = "tessera-sync-"
_ACCESS_KEY_USERNAME: Final[str] = "access_key_id"
_SECRET_KEY_USERNAME: Final[str] = "secret_access_key"


class SyncNotConfiguredError(Exception):
    """No sync target has been configured for this vault.

    Raised by :func:`load_config` when the operator runs a sync
    command before ``tessera sync setup`` has populated the
    ``_meta`` rows. The CLI surfaces this as a one-line "run
    setup first" message rather than letting it leak as a generic
    KeyError or NoneType.
    """


class SyncCredentialsMissingError(Exception):
    """The access key / secret key are not in the OS keyring.

    Distinct from :class:`SyncNotConfiguredError` — the non-secret
    config is present (so ``tessera sync setup`` did run) but the
    keyring entry for this store is gone (operator cleared it,
    keyring backend changed, OS user changed). The CLI surfaces
    this as "rerun setup to re-enter credentials" rather than
    silently treating an empty access key as the authentication.
    """


@dataclass(frozen=True, slots=True)
class StoredConfig:
    """Non-secret sync target config persisted in ``_meta``.

    The secret material lives in the keyring; the CLI assembles
    a full :class:`S3Config` by combining this struct with the
    keyring-loaded credentials at command-execution time.
    """

    endpoint: str
    bucket: str
    region: str
    prefix: str


def save_config(conn: sqlcipher3.Connection, *, config: StoredConfig) -> None:
    """Persist the non-secret config to ``_meta``.

    Uses ``INSERT OR REPLACE`` so re-running ``tessera sync setup``
    against the same vault overwrites the prior config rather than
    accumulating stale rows. The keyring entry under the new
    store identity is the operator's responsibility — re-setup
    re-prompts for credentials separately.
    """

    rows = (
        (_META_PREFIX + "endpoint", config.endpoint),
        (_META_PREFIX + "bucket", config.bucket),
        (_META_PREFIX + "region", config.region),
        (_META_PREFIX + "prefix", config.prefix),
    )
    for key, value in rows:
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
            (key, value),
        )


def load_config(conn: sqlcipher3.Connection) -> StoredConfig:
    """Return the persisted sync config or raise."""

    found: dict[str, str] = {}
    rows = conn.execute(
        "SELECT key, value FROM _meta WHERE key LIKE ?",
        (_META_PREFIX + "%",),
    ).fetchall()
    for key, value in rows:
        if not key.startswith(_META_PREFIX):
            continue
        suffix = key[len(_META_PREFIX) :]
        if suffix in {"endpoint", "bucket", "region", "prefix"}:
            found[suffix] = value
    required = {"endpoint", "bucket", "region"}
    missing = required - set(found.keys())
    if missing:
        raise SyncNotConfiguredError(
            f"sync not configured (missing _meta keys: {sorted(missing)}); "
            f"run `tessera sync setup` first"
        )
    return StoredConfig(
        endpoint=found["endpoint"],
        bucket=found["bucket"],
        region=found["region"],
        prefix=found.get("prefix", ""),
    )


def clear_config(conn: sqlcipher3.Connection) -> None:
    """Remove the persisted sync config from ``_meta``.

    Watermarks are NOT cleared by this call — they live under
    distinct ``_meta`` keys (``sync_watermark_<store_id>``) and
    are managed via :mod:`tessera.sync.watermark`. The CLI's
    ``setup`` re-run path may want to clear watermarks too;
    deciding when to do so is policy, not mechanism, so keep
    this call narrow.
    """

    conn.execute(
        "DELETE FROM _meta WHERE key IN (?, ?, ?, ?)",
        (
            _META_PREFIX + "endpoint",
            _META_PREFIX + "bucket",
            _META_PREFIX + "region",
            _META_PREFIX + "prefix",
        ),
    )


def keyring_service_for(stored: StoredConfig) -> str:
    """Return the keyring ``service`` string for this store.

    Keyed by the same store_identity hash the watermark uses so
    one operator can manage credentials for multiple stores from
    one keyring backend without collision.
    """

    sid = store_identity(
        endpoint=stored.endpoint,
        bucket=stored.bucket,
        prefix=stored.prefix,
    )
    return f"{_KEYRING_SERVICE_PREFIX}{sid}"


def save_credentials(
    *,
    stored: StoredConfig,
    access_key_id: str,
    secret_access_key: str,
) -> None:
    """Persist credentials to the OS keyring under the store entry.

    Two distinct keyring entries (access-key + secret-key) so a
    backend that pads keys, redacts logs by username, or otherwise
    treats username as a label can render a useful identifier.
    Failures bubble up as :class:`keyring_cache.KeyringUnavailableError`.
    """

    service = keyring_service_for(stored)
    keyring_cache.store_password(service, _ACCESS_KEY_USERNAME, access_key_id)
    keyring_cache.store_password(service, _SECRET_KEY_USERNAME, secret_access_key)


def load_credentials(*, stored: StoredConfig) -> tuple[str, str]:
    """Return ``(access_key_id, secret_access_key)`` from the keyring.

    Raises :class:`SyncCredentialsMissingError` when either entry
    is absent. A partially-present pair (one entry present, one
    missing) is treated as missing — a half-credential cannot
    authenticate against S3 anyway, so the caller must rerun
    setup either way.
    """

    service = keyring_service_for(stored)
    access = keyring_cache.load_password(service, _ACCESS_KEY_USERNAME)
    secret = keyring_cache.load_password(service, _SECRET_KEY_USERNAME)
    if access is None or secret is None:
        raise SyncCredentialsMissingError(
            f"credentials missing from keyring service {service!r}; "
            f"rerun `tessera sync setup` to re-enter them"
        )
    return access, secret


def clear_credentials(*, stored: StoredConfig) -> None:
    """Remove the credential entries from the OS keyring.

    Idempotent — keyring backends that raise on "delete missing
    entry" are caught by ``keyring_cache.clear_password``'s
    PasswordDeleteError handling.
    """

    service = keyring_service_for(stored)
    keyring_cache.clear_password(service, _ACCESS_KEY_USERNAME)
    keyring_cache.clear_password(service, _SECRET_KEY_USERNAME)


def assemble_s3_config(
    *,
    stored: StoredConfig,
    access_key_id: str,
    secret_access_key: str,
) -> S3Config:
    """Combine the persisted non-secret config with credentials.

    Wrapping the assembly in a function rather than letting CLI
    code construct ``S3Config`` directly keeps one place to add a
    future field (e.g., explicit signing region distinct from the
    bucket region for multi-region access points).
    """

    return S3Config(
        endpoint=stored.endpoint,
        bucket=stored.bucket,
        region=stored.region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        prefix=stored.prefix,
    )


__all__ = [
    "StoredConfig",
    "SyncCredentialsMissingError",
    "SyncNotConfiguredError",
    "assemble_s3_config",
    "clear_config",
    "clear_credentials",
    "keyring_service_for",
    "load_config",
    "load_credentials",
    "save_config",
    "save_credentials",
]
