"""Pull primitive for V0.5-P9 BYO sync.

A pull resolves the latest manifest from the BlobStore, verifies
its signature under the local master key, enforces sequence
monotonicity against the local watermark (replay defence),
fetches the matching encrypted blob, recomputes the blob's
content hash to confirm it matches the signed manifest, unwraps
the DEK, decrypts the blob, and writes the recovered SQLCipher
file to ``target_path``.

After the file lands, the caller-side flow re-opens the vault
under the master key and runs ``tessera audit verify`` (or its
storage-layer equivalent ``verify_chain``). The recovered
chain head must match ``manifest.audit_chain_head`` or the pull
is treated as integrity-broken.

The pull never writes to ``target_path`` until every check has
passed: signature verify, sequence monotonicity, blob_id match,
DEK unwrap, and ciphertext authentication. Failures abort with a
typed exception and leave the target untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tessera.sync import envelope
from tessera.sync.manifest import (
    InvalidManifestError,
    Manifest,
    ReplayedManifestError,
    check_sequence_monotonic,
    parse_manifest,
    verify_signature,
)
from tessera.sync.storage import BlobStore, ManifestNotFoundError, compute_blob_id


class PullError(Exception):
    """Base class for pull failures."""


class NoManifestAvailableError(PullError):
    """The store has no manifests — nothing to pull."""


class BlobIntegrityError(PullError):
    """The fetched blob's sha256 does not match the manifest's blob_id."""


class VaultIdMismatchError(PullError):
    """The manifest's vault_id does not match the pinned target vault_id."""


@dataclass(frozen=True, slots=True)
class PullResult:
    """Summary of one successful pull, returned before the chain re-verify."""

    sequence_number: int
    blob_id: str
    bytes_written: int
    audit_chain_head: str
    vault_id: str
    schema_version: int


def pull(
    *,
    store: BlobStore,
    target_path: Path,
    master_key: bytes,
    last_restored_sequence: int = 0,
    expected_vault_id: str | None = None,
) -> PullResult:
    """Restore the latest snapshot from ``store`` to ``target_path``.

    ``last_restored_sequence`` is the local watermark — the
    sequence number of the most recent successful pull. A pull
    whose manifest sequence is at or below this watermark is
    rejected as a replay.

    ``expected_vault_id`` pins the target vault's id when the
    caller has a prior expectation. Pull rejects on mismatch
    rather than silently overwriting one vault with another's
    snapshot — common cause: the BlobStore root was reused across
    two distinct vaults by mistake.

    The target file is overwritten atomically via tmp + rename so
    a crash mid-write never leaves a half-written SQLCipher file
    that would refuse to open.
    """

    latest = store.latest_manifest_sequence()
    if latest is None:
        raise NoManifestAvailableError("store has no manifests; nothing to pull")
    try:
        raw = store.get_manifest(latest)
    except ManifestNotFoundError as exc:
        # Race against a concurrent prune that deleted the manifest
        # between list and read. Surface as the same not-available
        # error since the operator action is identical.
        raise NoManifestAvailableError(
            f"manifest sequence {latest} disappeared between list and read"
        ) from exc

    manifest = parse_manifest(raw)
    verify_signature(manifest, master_key=master_key)
    check_sequence_monotonic(incoming=manifest, last_restored_sequence=last_restored_sequence)
    if expected_vault_id is not None and manifest.vault_id != expected_vault_id:
        raise VaultIdMismatchError(
            f"manifest vault_id {manifest.vault_id!r} != expected "
            f"{expected_vault_id!r}; refusing to overwrite a different vault"
        )

    ciphertext = store.get_blob(manifest.blob_id)
    actual_blob_id = compute_blob_id(ciphertext)
    if actual_blob_id != manifest.blob_id:
        # The signed manifest binds blob_id but not the bytes
        # directly. Recomputing the hash and comparing to the
        # signed value is the explicit binding step. A mismatch
        # means the BlobStore returned different bytes than the
        # push wrote — provider tampering or filesystem
        # corruption.
        raise BlobIntegrityError(
            f"blob {manifest.blob_id!r} hash mismatch (stored "
            f"{actual_blob_id!r}); refusing to decrypt"
        )

    wrapped = manifest.wrapped_key()
    dek = envelope.unwrap_dek(master_key=master_key, wrapped=wrapped)
    blob = envelope.EncryptedBlob(
        nonce=manifest.encrypted_blob().nonce,
        ciphertext=ciphertext,
    )
    plaintext = envelope.decrypt_blob(dek=dek, blob=blob)

    # Atomic write so a crash mid-restore never leaves a
    # half-written SQLCipher file. The .salt sidecar lives next to
    # the vault and is the caller's responsibility — the target
    # vault must already have its salt set up for the master key
    # to derive correctly.
    tmp = target_path.with_suffix(target_path.suffix + ".tmp")
    tmp.write_bytes(plaintext)
    tmp.replace(target_path)

    return PullResult(
        sequence_number=manifest.sequence_number,
        blob_id=manifest.blob_id,
        bytes_written=len(plaintext),
        audit_chain_head=manifest.audit_chain_head,
        vault_id=manifest.vault_id,
        schema_version=manifest.schema_version,
    )


def fetch_manifest(
    *,
    store: BlobStore,
    sequence_number: int | None = None,
) -> Manifest:
    """Read a manifest from the store without performing a pull.

    Useful for status / inspection commands that want to display
    the latest sequence, audit_chain_head, or pushed_at_epoch
    without actually restoring the vault.

    Raises :class:`InvalidManifestError` on parse failure (delegates
    to :func:`parse_manifest`). Does NOT verify the signature —
    callers that need authenticated metadata invoke
    :func:`verify_signature` separately so failure shapes stay
    distinct (parse vs signature).
    """

    if sequence_number is None:
        latest = store.latest_manifest_sequence()
        if latest is None:
            raise NoManifestAvailableError("store has no manifests")
        sequence_number = latest
    try:
        raw = store.get_manifest(sequence_number)
    except ManifestNotFoundError as exc:
        raise NoManifestAvailableError(str(exc)) from exc
    return parse_manifest(raw)


__all__ = [
    "BlobIntegrityError",
    "InvalidManifestError",
    "NoManifestAvailableError",
    "PullError",
    "PullResult",
    "ReplayedManifestError",
    "VaultIdMismatchError",
    "fetch_manifest",
    "pull",
]
