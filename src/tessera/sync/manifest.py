"""Signed manifest format for BYO sync (V0.5-P9).

Each push writes one ``EncryptedBlob`` to the BlobStore plus one
manifest. The manifest carries everything the pull side needs to
verify integrity and decrypt the blob:

* ``vault_id`` — the source vault's stable id from ``_meta``. Pull
  rejects manifests whose vault_id does not match the target if a
  pinned id is supplied.
* ``sequence_number`` — monotonically increasing per push. Pull
  rejects manifests whose sequence is at or below the last
  successfully restored sequence (replay defence).
* ``audit_chain_head`` — the source vault's audit-chain head row
  hash at push time. Re-verifying the chain on the restored vault
  must match this value or the restore is treated as integrity-
  broken.
* ``schema_version`` — the SQLite schema version the source vault
  was running. Cross-version restore is out of scope at V0.5; pull
  rejects on mismatch.
* ``blob_id`` — the BlobStore key for the encrypted vault payload.
* ``blob_nonce`` — the AES-GCM nonce used to encrypt the blob.
* ``wrapped_dek`` — the DEK sealed under the master key.
* ``signature`` — HMAC-SHA256 over the canonical_json of every
  field above, keyed by the master key. Tampering with any field
  surfaces as a signature-mismatch error on pull.

Canonical JSON for the signature input reuses ``vault.canonical_json``
so the byte sequence is byte-stable across runs and platforms —
exactly the same primitive the audit chain uses, so the V0.5-P9
manifest signature inherits the V0.5-P8 determinism contract.

The signature does not commit to the ciphertext bytes themselves:
the AES-GCM tag inside the ciphertext provides authentication for
the data. The manifest signature commits to the *blob_id* (a hash
or stable identifier the BlobStore returns), which transitively
binds the ciphertext via the BlobStore's content-addressed
storage. Local filesystem implementations use a sha256 of the
ciphertext as the blob_id so the signature ↔ data binding is
explicit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Final

from tessera.sync.envelope import WrappedKey
from tessera.vault.canonical_json import canonical_json

MANIFEST_VERSION: Final[int] = 1

# Sentinel emitted into ``audit_chain_head`` when the source vault
# has no audit rows at push time. Real audit-chain row hashes are
# 64-character lowercase hex (sha256); this sentinel cannot collide
# (contains a colon, only 12 chars). A distinguished value rather
# than the empty string forecloses the "if x: verify_chain(...)"
# unsafe-caller pattern that would silently skip integrity
# enforcement on a forged empty-vault manifest. The sentinel is
# part of the signed payload so an attacker cannot substitute it
# without breaking the HMAC.
EMPTY_CHAIN_SENTINEL: Final[str] = "sha256:empty"


class ManifestError(Exception):
    """Base class for manifest validation failures."""


class InvalidManifestError(ManifestError):
    """Raw manifest JSON does not parse against the V0.5-P9 contract."""


class InvalidSignatureError(ManifestError):
    """Manifest signature did not verify under the supplied master key."""


class ReplayedManifestError(ManifestError):
    """Manifest sequence number regresses against the stored watermark."""


@dataclass(frozen=True, slots=True)
class Manifest:
    """A signed sync manifest.

    Fields are flat-typed so the JSON serialisation is the
    canonical_json output without any per-field mapping logic. The
    signature is excluded from the signing input by design — it is
    appended after the canonical bytes are computed.
    """

    manifest_version: int
    vault_id: str
    sequence_number: int
    schema_version: int
    audit_chain_head: str
    blob_id: str
    blob_nonce_b64: str
    wrapped_dek_nonce_b64: str
    wrapped_dek_b64: str
    pushed_at_epoch: int
    signature_b64: str = field(default="")

    def to_json_bytes(self) -> bytes:
        """Serialise the full manifest (including signature) for the wire."""

        return canonical_json(self._as_dict_with_signature())

    def _as_dict_with_signature(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "vault_id": self.vault_id,
            "sequence_number": self.sequence_number,
            "schema_version": self.schema_version,
            "audit_chain_head": self.audit_chain_head,
            "blob_id": self.blob_id,
            "blob_nonce_b64": self.blob_nonce_b64,
            "wrapped_dek_nonce_b64": self.wrapped_dek_nonce_b64,
            "wrapped_dek_b64": self.wrapped_dek_b64,
            "pushed_at_epoch": self.pushed_at_epoch,
            "signature_b64": self.signature_b64,
        }

    def _signing_payload(self) -> bytes:
        """Canonical bytes the signature commits to (signature excluded)."""

        payload = self._as_dict_with_signature()
        payload.pop("signature_b64")
        return canonical_json(payload)

    def blob_nonce(self) -> bytes:
        """Decode the AES-GCM nonce used for the vault-blob encrypt.

        The ciphertext bytes themselves live in the BlobStore under
        ``blob_id``; the pull side fetches them separately and
        constructs an :class:`EncryptedBlob` with this nonce paired
        against the fetched bytes. Returning the raw nonce (not a
        half-built ``EncryptedBlob`` with empty ciphertext) avoids
        the partial-construction footgun a future caller could trip
        on.
        """

        return base64.b64decode(self.blob_nonce_b64)

    def wrapped_key(self) -> WrappedKey:
        """Reconstruct the wrapped DEK for ``unwrap_dek``."""

        return WrappedKey(
            nonce=base64.b64decode(self.wrapped_dek_nonce_b64),
            ciphertext=base64.b64decode(self.wrapped_dek_b64),
        )


def build_manifest(
    *,
    vault_id: str,
    sequence_number: int,
    schema_version: int,
    audit_chain_head: str,
    blob_id: str,
    blob_nonce: bytes,
    wrapped: WrappedKey,
    pushed_at_epoch: int,
    master_key: bytes,
) -> Manifest:
    """Assemble + sign a manifest for one push.

    Sequence-monotonicity is the caller's responsibility: this
    function does not consult the BlobStore's existing manifests.
    The push primitive in :mod:`tessera.sync.push` resolves the
    next sequence by reading the latest stored manifest (or zero
    if none) before calling here.
    """

    if sequence_number < 1:
        raise InvalidManifestError(f"sequence_number must be >= 1, got {sequence_number}")
    unsigned = Manifest(
        manifest_version=MANIFEST_VERSION,
        vault_id=vault_id,
        sequence_number=sequence_number,
        schema_version=schema_version,
        audit_chain_head=audit_chain_head,
        blob_id=blob_id,
        blob_nonce_b64=base64.b64encode(blob_nonce).decode("ascii"),
        wrapped_dek_nonce_b64=base64.b64encode(wrapped.nonce).decode("ascii"),
        wrapped_dek_b64=base64.b64encode(wrapped.ciphertext).decode("ascii"),
        pushed_at_epoch=pushed_at_epoch,
    )
    signature = _compute_signature(unsigned, master_key)
    return Manifest(
        manifest_version=unsigned.manifest_version,
        vault_id=unsigned.vault_id,
        sequence_number=unsigned.sequence_number,
        schema_version=unsigned.schema_version,
        audit_chain_head=unsigned.audit_chain_head,
        blob_id=unsigned.blob_id,
        blob_nonce_b64=unsigned.blob_nonce_b64,
        wrapped_dek_nonce_b64=unsigned.wrapped_dek_nonce_b64,
        wrapped_dek_b64=unsigned.wrapped_dek_b64,
        pushed_at_epoch=unsigned.pushed_at_epoch,
        signature_b64=signature,
    )


def parse_manifest(raw: bytes) -> Manifest:
    """Decode a manifest from its wire bytes.

    Does **not** verify the signature — :func:`verify_signature`
    is the explicit step the pull side runs after parse so failures
    surface with the exact reason (parse vs signature vs replay)
    rather than collapsing into one opaque error.
    """

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise InvalidManifestError("manifest must be a JSON object")
    expected_keys = {
        "manifest_version",
        "vault_id",
        "sequence_number",
        "schema_version",
        "audit_chain_head",
        "blob_id",
        "blob_nonce_b64",
        "wrapped_dek_nonce_b64",
        "wrapped_dek_b64",
        "pushed_at_epoch",
        "signature_b64",
    }
    missing = expected_keys - set(decoded.keys())
    if missing:
        raise InvalidManifestError(f"manifest missing required keys {sorted(missing)}")
    extra = set(decoded.keys()) - expected_keys
    if extra:
        raise InvalidManifestError(f"manifest carries unknown keys {sorted(extra)}")
    if decoded["manifest_version"] != MANIFEST_VERSION:
        raise InvalidManifestError(
            f"manifest_version {decoded['manifest_version']} != supported {MANIFEST_VERSION}"
        )
    try:
        return Manifest(
            manifest_version=int(decoded["manifest_version"]),
            vault_id=str(decoded["vault_id"]),
            sequence_number=int(decoded["sequence_number"]),
            schema_version=int(decoded["schema_version"]),
            audit_chain_head=str(decoded["audit_chain_head"]),
            blob_id=str(decoded["blob_id"]),
            blob_nonce_b64=str(decoded["blob_nonce_b64"]),
            wrapped_dek_nonce_b64=str(decoded["wrapped_dek_nonce_b64"]),
            wrapped_dek_b64=str(decoded["wrapped_dek_b64"]),
            pushed_at_epoch=int(decoded["pushed_at_epoch"]),
            signature_b64=str(decoded["signature_b64"]),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidManifestError(f"manifest field type error: {exc}") from exc


def verify_signature(manifest: Manifest, *, master_key: bytes) -> None:
    """Verify the manifest signature under ``master_key``.

    Raises :class:`InvalidSignatureError` on mismatch. Uses
    constant-time comparison via :func:`hmac.compare_digest` so a
    timing oracle on the master key is closed.
    """

    expected = _compute_signature(manifest, master_key)
    if not hmac.compare_digest(expected, manifest.signature_b64):
        raise InvalidSignatureError(
            "manifest signature does not match (wrong master key or tampered manifest)"
        )


def check_sequence_monotonic(
    *,
    incoming: Manifest,
    last_restored_sequence: int,
) -> None:
    """Enforce the monotonic-sequence invariant on pull.

    A pull whose manifest sequence is at or below the last
    successfully restored sequence is a replay attempt: the
    BlobStore could be returning an older snapshot to revert the
    target vault to a prior state. Reject before decrypting so the
    target vault is never even partially overwritten.
    """

    if incoming.sequence_number <= last_restored_sequence:
        raise ReplayedManifestError(
            f"manifest sequence {incoming.sequence_number} <= last restored "
            f"{last_restored_sequence}; refusing to restore an older snapshot"
        )


def _compute_signature(manifest: Manifest, master_key: bytes) -> str:
    payload = manifest._signing_payload()
    digest = hmac.new(master_key, payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


__all__ = [
    "EMPTY_CHAIN_SENTINEL",
    "MANIFEST_VERSION",
    "InvalidManifestError",
    "InvalidSignatureError",
    "Manifest",
    "ManifestError",
    "ReplayedManifestError",
    "build_manifest",
    "check_sequence_monotonic",
    "parse_manifest",
    "verify_signature",
]
