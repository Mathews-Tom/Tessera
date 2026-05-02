"""Push primitive for V0.5-P9 BYO sync.

A push reads the encrypted vault file from disk, encrypts it
again under a fresh DEK with AES-GCM, wraps the DEK under the
master key, builds a signed manifest carrying the audit-chain
head + monotonic sequence, and stores blob + manifest in the
caller-supplied :class:`BlobStore`.

The vault stays at rest under SQLCipher's per-page encryption;
the second crypto layer adds the single-tag-over-the-file
property + a binding to the audit chain head + replay defence
(see ``vault/sync/__init__.py`` for the rationale).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlcipher3

from tessera.sync import envelope
from tessera.sync import manifest as sync_manifest
from tessera.sync.manifest import EMPTY_CHAIN_SENTINEL
from tessera.sync.storage import BlobStore, compute_blob_id
from tessera.vault.audit_chain import AuditChainBrokenError, verify_chain


class PushError(Exception):
    """Base class for push failures."""


class PushChainBreakError(PushError):
    """Audit chain on the source vault did not verify before push.

    Pushing a vault whose chain is broken would propagate a
    corrupt snapshot to the BlobStore and pin the corruption into
    the manifest's ``audit_chain_head`` field. Pull would then
    succeed, re-verify on the restored vault, and report the same
    breakage — the corruption ends up downstream. Refusing to
    push is the safer default; the operator must run
    ``tessera audit verify`` and resolve the breakage before
    re-pushing.
    """


@dataclass(frozen=True, slots=True)
class PushResult:
    """Summary of one successful push."""

    sequence_number: int
    blob_id: str
    bytes_uploaded: int
    audit_chain_head: str


def push(
    *,
    vault_path: Path,
    conn: sqlcipher3.Connection,
    store: BlobStore,
    master_key: bytes,
    pushed_at_epoch: int | None = None,
) -> PushResult:
    """Push a snapshot of ``vault_path`` to ``store``.

    The function is read-only against the source vault: it opens
    the file as bytes for encryption rather than streaming through
    the SQLCipher connection. ``conn`` is consulted only for the
    audit-chain head verification and the vault_id / schema_version
    metadata.

    Sequence-monotonicity is enforced relative to the store: the
    next sequence is ``latest_manifest_sequence() + 1``. A push
    against a fresh store (no manifests) produces sequence ``1``.
    """

    vault_id, schema_version = _read_meta(conn)
    # ``verify_chain`` raises AuditChainBrokenError on any chain
    # breakage. Wrap it as PushChainBreakError so the sync surface
    # presents one typed exception family for chain failures
    # rather than leaking the audit-layer name across the
    # boundary. ``from exc`` preserves the underlying cause for
    # operator diagnostics.
    try:
        chain_outcome = verify_chain(conn)
    except AuditChainBrokenError as exc:
        raise PushChainBreakError(f"source vault audit chain failed verification: {exc}") from exc
    # An empty vault has no audit chain head to bind. Pushing one
    # is technically meaningful (snapshot of a fresh vault) but
    # the manifest signature still needs a stable value. The
    # ``EMPTY_CHAIN_SENTINEL`` constant is unmistakable (contains
    # a colon, 12 chars vs the 64-char hex of a real row hash) so
    # a future caller cannot accidentally treat it as "skip
    # verify_chain on restore" via an ``if x:`` truthy check.
    chain_head = EMPTY_CHAIN_SENTINEL if chain_outcome.head is None else chain_outcome.head.row_hash

    plaintext = vault_path.read_bytes()
    dek = envelope.generate_dek()
    blob = envelope.encrypt_blob(dek=dek, plaintext=plaintext)
    wrapped = envelope.wrap_dek(master_key=master_key, dek=dek)

    blob_id = compute_blob_id(blob.ciphertext)
    next_sequence = (store.latest_manifest_sequence() or 0) + 1
    push_epoch = (
        pushed_at_epoch if pushed_at_epoch is not None else int(datetime.now(UTC).timestamp())
    )

    signed = sync_manifest.build_manifest(
        vault_id=vault_id,
        sequence_number=next_sequence,
        schema_version=schema_version,
        audit_chain_head=chain_head,
        blob_id=blob_id,
        blob_nonce=blob.nonce,
        wrapped=wrapped,
        pushed_at_epoch=push_epoch,
        master_key=master_key,
    )

    # Store blob first so a crash between blob and manifest leaves
    # an orphan blob (harmless; reusable on retry) rather than a
    # manifest pointing at a missing blob (broken state). Manifest
    # is the index of record — its presence is what the pull side
    # treats as "this push completed".
    store.put_blob(blob_id, blob.ciphertext)
    store.put_manifest(signed.sequence_number, signed.to_json_bytes())

    return PushResult(
        sequence_number=signed.sequence_number,
        blob_id=blob_id,
        bytes_uploaded=len(blob.ciphertext),
        audit_chain_head=chain_head,
    )


def _read_meta(conn: sqlcipher3.Connection) -> tuple[str, int]:
    """Read ``vault_id`` + ``schema_version`` from ``_meta`` directly.

    Avoids depending on the private ``_read_state`` helper in
    ``vault.connection``; the push path operates on a raw
    ``sqlcipher3.Connection`` (the daemon owns the higher-level
    ``VaultConnection`` wrapper) so going to SQL directly keeps
    the dependency surface narrow.
    """

    rows = dict(
        conn.execute(
            "SELECT key, value FROM _meta WHERE key IN ('vault_id', 'schema_version')"
        ).fetchall()
    )
    vault_id = str(rows.get("vault_id") or "")
    schema_version_raw = rows.get("schema_version")
    if not vault_id:
        raise PushError("source vault has no vault_id in _meta")
    if schema_version_raw is None:
        raise PushError("source vault has no schema_version in _meta")
    return vault_id, int(schema_version_raw)


__all__ = [
    "PushChainBreakError",
    "PushError",
    "PushResult",
    "push",
]
