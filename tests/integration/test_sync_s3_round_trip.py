"""V0.5-P9b S3 round-trip integration tests.

Re-runs the V0.5-P9 part 1 exit-gate scenario against the S3
adapter: push a populated vault → S3-fake backend → pull on a
separate target → re-open under the master key → run
``verify_chain`` → manifest's signed audit_chain_head matches
restored chain head.

The fake S3 backend is the same one :mod:`tests.unit.test_sync_s3`
uses (in-process httpx MockTransport, hand-rolled per ADR-0022
§Alternatives). Running the full security + round-trip suite
against it proves the BlobStore protocol contract is uniform
across both backends — the V0.5-P9 part 1 LocalFilesystemStore
suite is the reference; the S3 adapter inherits-by-protocol.

Tests here cover the gate-of-record properties:
1. Round-trip preserves chain integrity.
2. Tampered blob aborts pull (provider-modify defence).
3. Replayed manifest aborts pull.
4. Forged manifest signature aborts pull.
5. Cross-vault overwrite refused.

Per-mechanism tests for the S3 wire surface (URL shape, signing,
pagination, error mapping) live in :mod:`tests.unit.test_sync_s3`
where the fake backend is exercised directly without the
SQLCipher / vault overhead.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import sqlcipher3

from tessera.migration import bootstrap
from tessera.sync.manifest import (
    InvalidSignatureError,
    ReplayedManifestError,
)
from tessera.sync.pull import (
    BlobIntegrityError,
    VaultIdMismatchError,
    pull,
)
from tessera.sync.push import push
from tessera.sync.s3 import S3BlobStore, S3Config
from tessera.vault.audit_chain import verify_chain
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, new_salt, save_salt

# Reuse the fake backend implementation from the unit test module.
# Keeping one canonical fake means a fix-or-feature in the wire
# emulation lands in one place, not two.
from tests.unit.test_sync_s3 import _FakeS3Backend


@pytest.fixture
def passphrase() -> bytes:
    return b"correct horse battery staple for s3 sync"


def _bootstrap_vault(path: Path, passphrase: bytes) -> tuple[ProtectedKey, bytes]:
    salt = new_salt()
    key = derive_key(passphrase, salt)
    bootstrap(path, key)
    return key, salt


def _seed_some_audit_rows(conn: sqlcipher3.Connection) -> None:
    from tessera.vault import capture

    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01S3SYNC', 's3-test', 0)"
    )
    agent_id = int(conn.execute("SELECT id FROM agents WHERE external_id='01S3SYNC'").fetchone()[0])
    for i in range(3):
        capture.capture(
            conn,
            agent_id=agent_id,
            facet_type="project",
            content=f"s3 round-trip seed facet {i}",
            source_tool="cli",
            captured_at=1_700_000_000 + i,
        )


@pytest.fixture
def populated_vault(tmp_path: Path, passphrase: bytes) -> tuple[Path, ProtectedKey, bytes]:
    src = tmp_path / "src.db"
    key, salt = _bootstrap_vault(src, passphrase)
    with VaultConnection.open(src, key) as vc:
        _seed_some_audit_rows(vc.connection)
    return src, key, salt


def _make_store(backend: _FakeS3Backend) -> S3BlobStore:
    config = S3Config(
        endpoint="https://s3.us-east-1.amazonaws.com",
        bucket="tessera-test-bucket",
        region="us-east-1",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    )
    transport = httpx.MockTransport(backend.handler())
    return S3BlobStore(config, transport=transport)


@pytest.fixture
def backend() -> _FakeS3Backend:
    fake = _FakeS3Backend()
    fake.add_bucket("tessera-test-bucket")
    return fake


@pytest.mark.integration
def test_s3_push_pull_round_trip_preserves_chain(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    passphrase: bytes,
    backend: _FakeS3Backend,
) -> None:
    """The v0.5 exit-gate scenario, against S3: a populated vault
    pushes through the S3 adapter to the in-process fake backend,
    pulls onto a separate path, re-opens under the master key, and
    the restored chain head matches the manifest's signed
    ``audit_chain_head``. This is the ADR-0022 D2 protocol-
    conformance commitment expressed as a test."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())

    with _make_store(backend) as store, VaultConnection.open(src, key) as vc:
        push_result = push(
            vault_path=src,
            conn=vc.connection,
            store=store,
            master_key=master_key_bytes,
            pushed_at_epoch=1_700_001_000,
        )
        pull_result = pull(
            store=store,
            target_path=dst,
            master_key=master_key_bytes,
        )

    assert push_result.sequence_number == 1
    assert pull_result.sequence_number == 1
    assert pull_result.audit_chain_head == push_result.audit_chain_head

    key2 = derive_key(passphrase, salt)
    with VaultConnection.open(dst, key2) as vc_restored:
        outcome = verify_chain(vc_restored.connection)
        assert outcome.head is not None
        assert outcome.head.row_hash == push_result.audit_chain_head


@pytest.mark.integration
def test_s3_byte_identity(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    backend: _FakeS3Backend,
) -> None:
    """Restored bytes match source exactly. Anchors the
    release-spec DoD wording 'restore on second machine →
    identical state' against the S3 backend."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())
    src_bytes_before = src.read_bytes()

    with _make_store(backend) as store, VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)
        pull(store=store, target_path=dst, master_key=master_key_bytes)

    assert dst.read_bytes() == src_bytes_before


@pytest.mark.integration
def test_s3_three_push_pull_cycles_advance_sequence(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    backend: _FakeS3Backend,
) -> None:
    """Three back-to-back push/pull cycles each advance the
    sequence by one. The watermark argument feeds the prior
    sequence in so the replay-defence accepts the next."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())

    last_seq = 0
    with _make_store(backend) as store:
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


@pytest.mark.security
def test_s3_pull_rejects_tampered_blob(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    backend: _FakeS3Backend,
) -> None:
    """Provider-modify defence over S3: tamper a byte in the
    stored ciphertext, recomputed sha256 mismatch surfaces as
    BlobIntegrityError before decrypt."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())

    with _make_store(backend) as store, VaultConnection.open(src, key) as vc:
        result = push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    # Tamper inside the fake-backend bucket directly.
    blob_key = "blobs/" + result.blob_id
    bucket = backend.buckets["tessera-test-bucket"]
    original = bucket[blob_key]
    bucket[blob_key] = b"\xff" + original[1:]

    with _make_store(backend) as store, pytest.raises(BlobIntegrityError, match="hash mismatch"):
        pull(store=store, target_path=dst, master_key=master_key_bytes)
    assert not dst.exists()


@pytest.mark.security
def test_s3_pull_rejects_replayed_manifest(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    backend: _FakeS3Backend,
) -> None:
    """Replay defence over S3: pull with a watermark above the
    store's latest sequence rejects."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())

    with _make_store(backend) as store, VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    with _make_store(backend) as store, pytest.raises(ReplayedManifestError, match="refusing"):
        pull(
            store=store,
            target_path=dst,
            master_key=master_key_bytes,
            last_restored_sequence=5,
        )


@pytest.mark.security
def test_s3_pull_rejects_forged_manifest_signature(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    backend: _FakeS3Backend,
) -> None:
    """Tampering the signed audit_chain_head field in the stored
    manifest invalidates the HMAC under the master key. Pull aborts
    at the signature step, never reaching the decrypt path."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())

    with _make_store(backend) as store, VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    bucket = backend.buckets["tessera-test-bucket"]
    manifest_key = "manifests/1.json"
    payload = json.loads(bucket[manifest_key])
    payload["audit_chain_head"] = "f" * 64
    bucket[manifest_key] = json.dumps(payload).encode("utf-8")

    with _make_store(backend) as store, pytest.raises(InvalidSignatureError):
        pull(store=store, target_path=dst, master_key=master_key_bytes)
    assert not dst.exists()


@pytest.mark.security
def test_s3_pull_rejects_cross_vault_overwrite(
    populated_vault: tuple[Path, ProtectedKey, bytes],
    tmp_path: Path,
    backend: _FakeS3Backend,
) -> None:
    """expected_vault_id pin rejects an S3-stored snapshot that
    came from a different vault. Common cause: the operator reused
    a bucket prefix across two vaults by mistake."""

    src, key, salt = populated_vault
    dst = tmp_path / "restored.db"
    save_salt(dst, salt)
    master_key_bytes = bytes.fromhex(key.hex())

    with _make_store(backend) as store, VaultConnection.open(src, key) as vc:
        push(vault_path=src, conn=vc.connection, store=store, master_key=master_key_bytes)

    with (
        _make_store(backend) as store,
        pytest.raises(VaultIdMismatchError, match="refusing to overwrite"),
    ):
        pull(
            store=store,
            target_path=dst,
            master_key=master_key_bytes,
            expected_vault_id="01DIFFERENTVAULTIDXX1234567",
        )
