"""V0.5-P9 LocalFilesystemStore — BlobStore + atomic writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.sync.storage import (
    BlobNotFoundError,
    BlobStoreError,
    LocalFilesystemStore,
    ManifestNotFoundError,
    compute_blob_id,
)


@pytest.mark.unit
def test_compute_blob_id_is_sha256_of_ciphertext() -> None:
    import hashlib

    data = b"the encrypted payload"
    assert compute_blob_id(data) == hashlib.sha256(data).hexdigest()


@pytest.mark.unit
def test_initialize_creates_layout(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "sync_root")
    store.initialize()
    assert (tmp_path / "sync_root" / "blobs").is_dir()
    assert (tmp_path / "sync_root" / "manifests").is_dir()


@pytest.mark.unit
def test_blob_round_trip(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    blob_id = compute_blob_id(b"abc")
    store.put_blob(blob_id, b"abc")
    assert store.get_blob(blob_id) == b"abc"


@pytest.mark.unit
def test_get_missing_blob_raises(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    with pytest.raises(BlobNotFoundError):
        store.get_blob("0" * 64)


@pytest.mark.unit
def test_manifest_round_trip(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    raw = b'{"manifest_version": 1}'
    store.put_manifest(7, raw)
    assert store.get_manifest(7) == raw


@pytest.mark.unit
def test_get_missing_manifest_raises(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    with pytest.raises(ManifestNotFoundError):
        store.get_manifest(99)


@pytest.mark.unit
def test_list_manifest_sequences_returns_sorted(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    for seq in (3, 1, 5, 2, 4):
        store.put_manifest(seq, b"x")
    assert store.list_manifest_sequences() == [1, 2, 3, 4, 5]
    assert store.latest_manifest_sequence() == 5


@pytest.mark.unit
def test_list_manifest_sequences_empty(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    assert store.list_manifest_sequences() == []
    assert store.latest_manifest_sequence() is None


@pytest.mark.unit
def test_list_ignores_provider_artefacts(tmp_path: Path) -> None:
    """Filesystem-synced backup folders carry .DS_Store, lock
    files, and other provider artefacts. The list operation must
    skip non-integer-stem files cleanly rather than raise."""

    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    store.put_manifest(2, b"x")
    (tmp_path / "manifests" / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "manifests" / "lock.tmp").write_bytes(b"junk")
    assert store.list_manifest_sequences() == [2]


@pytest.mark.unit
@pytest.mark.parametrize("bad_id", ["", "../escape", "a/b", "..", "a\\b"])
def test_blob_path_rejects_unsafe_id(bad_id: str, tmp_path: Path) -> None:
    """Path-traversal defence: a malformed blob_id must not escape
    the store root. Normal hex digests are the only legitimate
    shape today."""

    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    with pytest.raises(BlobStoreError, match="path-unsafe"):
        store.put_blob(bad_id, b"x")


@pytest.mark.unit
def test_manifest_path_rejects_zero_sequence(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    with pytest.raises(BlobStoreError, match="sequence_number"):
        store.put_manifest(0, b"x")


@pytest.mark.unit
def test_put_blob_is_atomic(tmp_path: Path) -> None:
    """Atomic write via tmp + rename: the tmp file disappears
    after a successful put. Ensures the store root never carries a
    half-written blob the pull side might read.
    """

    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    blob_id = compute_blob_id(b"data")
    store.put_blob(blob_id, b"data")
    blobs_dir = tmp_path / "blobs"
    leftovers = [p.name for p in blobs_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


@pytest.mark.unit
def test_overwriting_blob_is_idempotent(tmp_path: Path) -> None:
    """Re-pushing the same blob_id (same ciphertext) overwrites
    cleanly. Content-addressed storage means two pushes of
    byte-identical content target the same blob_id; the second
    put is a no-op in semantics even if it rewrites the file.
    """

    store = LocalFilesystemStore(tmp_path)
    store.initialize()
    blob_id = compute_blob_id(b"x")
    store.put_blob(blob_id, b"x")
    store.put_blob(blob_id, b"x")
    assert store.get_blob(blob_id) == b"x"
