"""BlobStore protocol + filesystem implementation for V0.5-P9.

A ``BlobStore`` is the abstract surface the push and pull
primitives target. V0.5-P9 ships ``LocalFilesystemStore`` —
filesystem-backed, suitable for filesystem-synced backup targets
(iCloud Drive, Dropbox, Syncthing, USB drive, NFS mount). V0.5-P9b
will add an S3 adapter that conforms to the same protocol and
inherits every crypto + manifest invariant the filesystem store
already exercises.

Layout under the store root:

    <root>/
      blobs/<blob_id>           # one encrypted vault payload per push
      manifests/<sequence>.json # one signed manifest per push

``blob_id`` is the sha256 of the ciphertext bytes — content-
addressed so the manifest signature transitively binds the
payload through ``blob_id``.

Manifests are sequence-numbered files so listing them gives a
deterministic, ordered view of the push history. Pull defaults to
the highest sequence; explicit restore-by-sequence is a future
addition (one of the V0.5-P9b CLI commands).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Protocol

_MANIFEST_SUFFIX: Final[str] = ".json"


class BlobStoreError(Exception):
    """Base class for BlobStore failures."""


class BlobNotFoundError(BlobStoreError):
    """Requested blob_id does not exist in the store."""


class ManifestNotFoundError(BlobStoreError):
    """No manifest exists at the requested sequence number."""


class BlobStore(Protocol):
    """The abstract surface push / pull target.

    Implementations are content-addressed for blobs (the caller
    supplies the sha256 of ciphertext) and sequence-addressed for
    manifests. Errors surface through the typed exceptions above
    so callers can distinguish "not synced yet" from "sync state
    corrupt" cleanly.
    """

    def put_blob(self, blob_id: str, ciphertext: bytes) -> None:
        """Store ``ciphertext`` at ``blob_id``. Idempotent."""
        ...

    def get_blob(self, blob_id: str) -> bytes:
        """Fetch the ciphertext at ``blob_id``.

        Raises :class:`BlobNotFoundError` when absent.
        """
        ...

    def put_manifest(self, sequence_number: int, raw: bytes) -> None:
        """Store the manifest bytes at ``sequence_number``."""
        ...

    def get_manifest(self, sequence_number: int) -> bytes:
        """Fetch the manifest bytes at ``sequence_number``.

        Raises :class:`ManifestNotFoundError` when absent.
        """
        ...

    def list_manifest_sequences(self) -> list[int]:
        """Return every present sequence number, sorted ascending."""
        ...

    def latest_manifest_sequence(self) -> int | None:
        """Convenience: highest sequence number present, or None."""
        ...


def compute_blob_id(ciphertext: bytes) -> str:
    """sha256 hex-digest of the ciphertext.

    Content-addressing the blob means the manifest's ``blob_id``
    field — which is signed by the master-key HMAC — transitively
    binds the ciphertext bytes. A swap of one blob for another
    surfaces immediately on pull when the recomputed hash does not
    match the manifest.
    """

    return hashlib.sha256(ciphertext).hexdigest()


class LocalFilesystemStore:
    """A BlobStore backed by a directory on the local filesystem.

    Useful when the BYO sync target is a filesystem-synced folder
    (iCloud Drive, Dropbox, Syncthing) rather than a true object
    store. The directory can be on a USB drive, an NFS mount, or
    any path the OS treats as a regular filesystem.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._blobs = root / "blobs"
        self._manifests = root / "manifests"

    def initialize(self) -> None:
        """Create the directory layout if missing.

        Separated from ``__init__`` so the BlobStore protocol can
        treat ``initialize`` as a one-shot setup step that does not
        run on every store instantiation. The push primitive calls
        this before its first put.
        """

        self._blobs.mkdir(parents=True, exist_ok=True)
        self._manifests.mkdir(parents=True, exist_ok=True)

    def put_blob(self, blob_id: str, ciphertext: bytes) -> None:
        self._blobs.mkdir(parents=True, exist_ok=True)
        target = self._blob_path(blob_id)
        # Atomic write via tmp + rename so a crash mid-write never
        # leaves a half-written blob the pull side might read. The
        # tmp suffix carries the blob_id prefix so concurrent puts
        # to different blobs do not collide.
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(ciphertext)
        tmp.replace(target)

    def get_blob(self, blob_id: str) -> bytes:
        target = self._blob_path(blob_id)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(f"blob {blob_id!r} not found under {self._blobs}") from exc

    def put_manifest(self, sequence_number: int, raw: bytes) -> None:
        self._manifests.mkdir(parents=True, exist_ok=True)
        target = self._manifest_path(sequence_number)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)

    def get_manifest(self, sequence_number: int) -> bytes:
        target = self._manifest_path(sequence_number)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise ManifestNotFoundError(
                f"manifest sequence {sequence_number} not found under {self._manifests}"
            ) from exc

    def list_manifest_sequences(self) -> list[int]:
        if not self._manifests.exists():
            return []
        return sorted(self._iter_manifest_sequences())

    def latest_manifest_sequence(self) -> int | None:
        sequences = self.list_manifest_sequences()
        return sequences[-1] if sequences else None

    def _iter_manifest_sequences(self) -> Iterator[int]:
        for entry in self._manifests.iterdir():
            if not entry.is_file() or entry.suffix != _MANIFEST_SUFFIX:
                continue
            try:
                yield int(entry.stem)
            except ValueError:
                # Ignore stray files with non-integer stems —
                # filesystem-synced backup folders sometimes carry
                # provider artefacts (.DS_Store, lock files) that
                # should not break a list operation.
                continue

    def _blob_path(self, blob_id: str) -> Path:
        # Reject path-traversal attempts at the boundary so a
        # malformed blob_id never escapes the store root. Hex
        # digests are the only legitimate blob_id shape today.
        if not blob_id or "/" in blob_id or ".." in blob_id or "\\" in blob_id:
            raise BlobStoreError(f"refusing path-unsafe blob_id {blob_id!r}")
        return self._blobs / blob_id

    def _manifest_path(self, sequence_number: int) -> Path:
        if sequence_number < 1:
            raise BlobStoreError(f"sequence_number must be >= 1, got {sequence_number}")
        return self._manifests / f"{sequence_number}{_MANIFEST_SUFFIX}"


__all__ = [
    "BlobNotFoundError",
    "BlobStore",
    "BlobStoreError",
    "LocalFilesystemStore",
    "ManifestNotFoundError",
    "compute_blob_id",
]
