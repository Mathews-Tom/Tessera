"""V0.5-P9 BYO sync round-trip integration + security tests.

Exercises the v0.5 exit-gate contract end-to-end:

    push a populated source vault → BlobStore →
        pull on a separate target path →
            re-open under master key →
                ``tessera audit verify`` clean →
                manifest's audit_chain_head == restored head.

Plus the threat-model §S6 mitigations:
    1. Tampered blob aborts pull (BlobIntegrityError on hash
       mismatch; InvalidCiphertextError if the manifest were
       tampered to match).
    2. Replayed manifest aborts pull (ReplayedManifestError on
       sequence regression).
    3. Forged manifest signature aborts pull (InvalidSignatureError).
    4. Cross-vault overwrite refused (VaultIdMismatchError).

The tests use a real SQLCipher vault bootstrapped from scratch so
the round-trip exercises the full crypto + chain stack.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlcipher3

from tessera.migration import bootstrap
from tessera.sync import envelope
from tessera.sync.manifest import (
    InvalidSignatureError,
    ReplayedManifestError,
)
from tessera.sync.pull import (
    BlobIntegrityError,
    NoManifestAvailableError,
    VaultIdMismatchError,
    pull,
)
from tessera.sync.push import push
from tessera.sync.storage import LocalFilesystemStore
from tessera.vault.audit_chain import verify_chain
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, new_salt, save_salt


@pytest.fixture
def passphrase() -> bytes:
    return b"correct horse battery staple for sync round-trip"


def _bootstrap_vault(path: Path, passphrase: bytes) -> tuple[ProtectedKey, bytes]:
    salt = new_salt()
    key = derive_key(passphrase, salt)
    bootstrap(path, key)
    return key, salt


def _seed_some_audit_rows(conn: sqlcipher3.Connection) -> None:
    """Populate the source vault with a few audit-bearing
    captures so the chain head is a real value, not the genesis
    sentinel. Uses agent + capture insertions."""

    from tessera.vault import capture

    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01SYNC1', 'sync-test', 0)"
    )
    agent_id = int(conn.execute("SELECT id FROM agents WHERE external_id='01SYNC1'").fetchone()[0])
    for i in range(3):
        capture.capture(
            conn,
            agent_id=agent_id,
            facet_type="project",
            content=f"sync round-trip seed facet {i}",
            source_tool="cli",
            captured_at=1_700_000_000 + i,
        )


@pytest.fixture
def populated_vault(tmp_path: Path, passphrase: bytes) -> tuple[Path, ProtectedKey, bytes]:
    """Bootstrapped vault seeded with audit rows so the chain
    head is a non-genesis value."""

    src = tmp_path / "src.db"
    key, salt = _bootstrap_vault(src, passphrase)
    with VaultConnection.open(src, key) as vc:
        _seed_some_audit_rows(vc.connection)
    return src, key, salt


@pytest.mark.integration
def test_push_pull_round_trip_preserves_chain(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    passphrase: bytes,
) -> None:
    """v0.5 exit-gate scenario: a populated vault round-trips
    through the BlobStore to a separate path; the restored vault
    re-opens under the master key, the chain re-verifies, and the
    head matches the manifest's signed audit_chain_head."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        push_result = push(
            vault_path=src,
            conn=vc.connection,
            store=store,
            master_key=master_key_bytes,
            pushed_at_epoch=1_700_001_000,
        )
    assert push_result.sequence_number == 1
    assert push_result.bytes_uploaded > 0

    pull_result = pull(
        store=store,
        target_path=dst,
        master_key=master_key_bytes,
    )
    assert pull_result.sequence_number == 1
    assert pull_result.audit_chain_head == push_result.audit_chain_head
    assert pull_result.bytes_written > 0

    key2 = derive_key(passphrase, salt)
    with VaultConnection.open(dst, key2) as vc_restored:
        outcome = verify_chain(vc_restored.connection)
        assert outcome.head is not None
        assert outcome.head.row_hash == push_result.audit_chain_head


@pytest.mark.integration
def test_push_increments_sequence_per_call(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    src, key, _salt = populated_vault
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        first = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)
        second = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)
        third = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)
    assert [first.sequence_number, second.sequence_number, third.sequence_number] == [1, 2, 3]


@pytest.mark.integration
def test_pull_with_no_manifest_raises(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "empty_store")
    store.initialize()
    with pytest.raises(NoManifestAvailableError):
        pull(
            store=store,
            target_path=tmp_path / "dst.db",
            master_key=b"\x00" * envelope.KEY_BYTES,
        )


@pytest.mark.security
def test_pull_rejects_tampered_blob(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """Threat-model §S6: provider modifies synced payload.
    Recomputed sha256 of the fetched ciphertext must match the
    signed blob_id; any flip surfaces as BlobIntegrityError
    before the decrypt step."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        result = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    # Flip a byte inside the stored ciphertext.
    blob_path = store_root / "blobs" / result.blob_id
    data = bytearray(blob_path.read_bytes())
    data[0] ^= 0xFF
    blob_path.write_bytes(bytes(data))

    with pytest.raises(BlobIntegrityError, match="hash mismatch"):
        pull(store=store, target_path=dst, master_key=master_key_bytes)
    # Target was never written.
    assert not dst.exists()


@pytest.mark.security
def test_pull_rejects_replayed_manifest(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """Threat-model §S6: replay of an older sync state. Pull must
    reject when the incoming sequence is at or below the local
    watermark."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    # Local watermark says we already restored sequence 5; the
    # store's latest is 2, which would regress us.
    with pytest.raises(ReplayedManifestError, match="refusing to restore"):
        pull(
            store=store,
            target_path=dst,
            master_key=master_key_bytes,
            last_restored_sequence=5,
        )
    assert not dst.exists()


@pytest.mark.security
def test_pull_rejects_forged_manifest_signature(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """A manifest whose signed fields were edited (and the
    signature recomputed under a different key, or simply
    flipped) must fail verification before the chain head is
    trusted."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    # Flip the signed audit_chain_head value in the stored
    # manifest. The attacker cannot recompute the signature
    # without the master key, so verification fails.
    manifest_path = store_root / "manifests" / "1.json"
    payload = json.loads(manifest_path.read_text())
    payload["audit_chain_head"] = "f" * 64
    manifest_path.write_text(json.dumps(payload))

    with pytest.raises(InvalidSignatureError):
        pull(store=store, target_path=dst, master_key=master_key_bytes)
    assert not dst.exists()


@pytest.mark.security
def test_pull_rejects_cross_vault_overwrite(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """Pulling with a pinned ``expected_vault_id`` that does not
    match the manifest's vault_id must refuse to overwrite. Common
    cause: BlobStore root reused across two distinct vaults by
    mistake."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    with pytest.raises(VaultIdMismatchError, match="refusing to overwrite"):
        pull(
            store=store,
            target_path=dst,
            master_key=master_key_bytes,
            expected_vault_id="01DIFFERENTVAULTIDXX1234567",
        )
    assert not dst.exists()


@pytest.mark.security
def test_pull_with_wrong_master_key_aborts_before_decrypt(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """A pull against the wrong master key must abort at the
    signature step, never reaching the decrypt path that would
    surface a misleading 'corrupt blob' error."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    wrong_key = bytes.fromhex(
        "00" * 32  # different 32-byte key, valid length for AES-256
    )
    with pytest.raises(InvalidSignatureError):
        pull(store=store, target_path=dst, master_key=wrong_key)
    assert not dst.exists()


@pytest.mark.integration
def test_round_trip_handles_increasing_pushes_with_per_blob_dedup(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """Three push/pull cycles: the latest manifest always wins,
    and each pull restores the byte-identical source vault from
    that point in time."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    last_seq = 0
    for cycle in range(3):
        with VaultConnection.open(src, key) as vc:
            push_result = push(
                vault_path=src,
                conn=vc.connection,
                store=store,
                master_key=master_key_bytes,
            )
            assert push_result.sequence_number == cycle + 1
        pull_result = pull(
            store=store,
            target_path=dst,
            master_key=master_key_bytes,
            last_restored_sequence=last_seq,
        )
        assert pull_result.sequence_number == cycle + 1
        last_seq = pull_result.sequence_number
        # Restored bytes match the source snapshot at push time.
        assert dst.read_bytes() == src.read_bytes()


@pytest.mark.security
def test_signed_manifest_binds_blob_id_to_ciphertext(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """A signed manifest that points at one blob_id but the store
    returns different bytes (provider swap) must fail the
    sha256 match before the decrypt step. Plant the swap by
    overwriting the blob with arbitrary bytes (not the original
    ciphertext)."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        result = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    blob_path = store_root / "blobs" / result.blob_id
    blob_path.write_bytes(b"completely different bytes")

    with pytest.raises(BlobIntegrityError, match="hash mismatch"):
        pull(store=store, target_path=dst, master_key=master_key_bytes)


@pytest.mark.integration
def test_push_pull_empty_vault_uses_sentinel(
    tmp_path: Path,
    passphrase: bytes,
) -> None:
    """V0.5-P9 sentinel round-trip: a freshly bootstrapped vault
    has no audit rows, so ``verify_chain`` returns head=None.
    Push must emit ``EMPTY_CHAIN_SENTINEL`` into the manifest
    rather than the empty string. Pull must surface it
    unchanged. A future caller that does ``if x:`` truthy check
    on the chain head will see a truthy value and try to verify
    against it, which is the right behaviour — failing loud is
    better than the silent-skip the empty-string sentinel
    would produce.
    """

    from tessera.sync.manifest import EMPTY_CHAIN_SENTINEL

    src = tmp_path / "src.db"
    dst = tmp_path / "restored.db"
    salt = new_salt()
    key = derive_key(passphrase, salt)
    bootstrap(src, key)
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    # Bootstrap writes a ``vault_init`` audit row at startup so a
    # freshly-bootstrapped vault always has a real chain head. To
    # exercise the empty-chain branch in push, truncate the audit
    # log first — this simulates a vault whose log was hard-
    # cleared by an external repair operation.
    with VaultConnection.open(src, key) as vc:
        vc.connection.execute("DELETE FROM audit_log")

    with VaultConnection.open(src, key) as vc:
        push_result = push(
            vault_path=src,
            conn=vc.connection,
            store=store,
            master_key=master_key_bytes,
        )

    assert push_result.audit_chain_head == EMPTY_CHAIN_SENTINEL

    pull_result = pull(
        store=store,
        target_path=dst,
        master_key=master_key_bytes,
    )
    assert pull_result.audit_chain_head == EMPTY_CHAIN_SENTINEL


@pytest.mark.integration
def test_round_trip_byte_identity(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """The restored file is byte-identical to the source at push
    time. Anchors the v0.5 exit-gate's 'identical state' wording
    in the release-spec DoD ('vault → S3-compatible bucket →
    restore on second machine → identical state')."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    src_bytes_before = src.read_bytes()

    with VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    pull(store=store, target_path=dst, master_key=master_key_bytes)
    assert dst.read_bytes() == src_bytes_before


@pytest.mark.security
def test_pull_failure_preserves_pre_existing_target(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """Pull writes the target via tmp + rename so a failure
    after the partial download cannot stomp a pre-existing
    file. Plant a known-bytes file at the target and prove
    every failure mode leaves it intact."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    sentinel_bytes = b"DO-NOT-OVERWRITE-PRE-EXISTING-TARGET"
    dst.write_bytes(sentinel_bytes)

    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        result = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    # Tamper the blob to force BlobIntegrityError.
    blob_path = store_root / "blobs" / result.blob_id
    data = bytearray(blob_path.read_bytes())
    data[0] ^= 0xFF
    blob_path.write_bytes(bytes(data))

    with pytest.raises(BlobIntegrityError):
        pull(store=store, target_path=dst, master_key=master_key_bytes)
    assert dst.read_bytes() == sentinel_bytes


@pytest.mark.security
def test_pull_rejects_tampered_blob_nonce(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
) -> None:
    """Tampering the manifest's ``blob_nonce_b64`` field
    invalidates the signature (the nonce is part of the signed
    payload). The pull side rejects at the signature step
    before the decrypt path runs — proves the nonce field
    binding survives the V0.5-P9 part 1 contract."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    store_root = tmp_path / "sync_store"
    store = LocalFilesystemStore(store_root)
    store.initialize()
    master_key_bytes = bytes.fromhex(key.hex())

    with VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    manifest_path = store_root / "manifests" / "1.json"
    payload = json.loads(manifest_path.read_text())
    import base64

    payload["blob_nonce_b64"] = base64.b64encode(b"\x00" * 12).decode()
    manifest_path.write_text(json.dumps(payload))

    with pytest.raises(InvalidSignatureError):
        pull(store=store, target_path=dst, master_key=master_key_bytes)
    assert not dst.exists()
