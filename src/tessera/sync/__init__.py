"""BYO cloud sync surface (V0.5-P9, ADR pending).

Snapshot-based sync of an entire SQLCipher-encrypted vault file
through a caller-configured object store, with a second crypto
layer on top of SQLCipher's at-rest encryption.

Why two crypto layers:

1. **Single tag over the file.** SQLCipher authenticates per-page;
   a tampered byte in a non-key page would not surface until that
   page is read. AES-GCM over the whole blob detects any flip
   immediately on pull.
2. **Wrapped DEK enables credential rotation.** A fresh data-
   encryption key per push is wrapped by the master key. Rotating
   sync credentials does not require re-encrypting the vault.
3. **Signed manifest binds sequence.** SQLCipher has no notion of
   "this file is newer than that one". A monotonic sequence
   number signed under the master key surfaces replay attacks
   that re-upload an older snapshot.

The sync module is a sibling of ``vault/`` rather than nested
under it because V0.5-P9b will add an S3 adapter that introduces
the only HTTP-client surface outside ``adapters/``. Keeping the
boundary local to ``sync/`` keeps the ``no-telemetry-grep`` CI
gate's allowlist tight.

This module ships **storage primitives only** at V0.5-P9. The S3
adapter, the ``setup`` / ``conflicts`` CLI commands, and the
multi-device row-merge semantics are V0.5-P9b. The
filesystem-backed ``LocalFilesystemStore`` is sufficient for
filesystem-synced backup targets (iCloud Drive, Dropbox,
Syncthing, USB drive, NFS mount) and exercises every crypto and
manifest invariant the S3 adapter will inherit.
"""
