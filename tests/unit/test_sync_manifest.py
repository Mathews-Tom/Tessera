"""V0.5-P9 manifest signing + parse + replay defence."""

from __future__ import annotations

import base64
import json
import os

import pytest

from tessera.sync import envelope
from tessera.sync import manifest as sync_manifest


def _canonical_inputs() -> dict[str, object]:
    master = os.urandom(envelope.KEY_BYTES)
    dek = envelope.generate_dek()
    wrapped = envelope.wrap_dek(master_key=master, dek=dek)
    blob = envelope.encrypt_blob(dek=dek, plaintext=b"vault bytes")
    return {
        "master_key": master,
        "vault_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "schema_version": 4,
        "audit_chain_head": "deadbeef" * 8,
        "blob_id": "0" * 64,
        "blob_nonce": blob.nonce,
        "wrapped": wrapped,
        "pushed_at_epoch": 1_700_000_000,
    }


@pytest.mark.unit
def test_build_and_verify_round_trip() -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=1,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    sync_manifest.verify_signature(signed, master_key=inputs["master_key"])  # type: ignore[arg-type]


@pytest.mark.unit
def test_verify_fails_under_wrong_master_key() -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=1,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    other = os.urandom(envelope.KEY_BYTES)
    with pytest.raises(sync_manifest.InvalidSignatureError):
        sync_manifest.verify_signature(signed, master_key=other)


@pytest.mark.unit
def test_parse_round_trip_via_json_bytes() -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=42,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    raw = signed.to_json_bytes()
    parsed = sync_manifest.parse_manifest(raw)
    assert parsed == signed
    sync_manifest.verify_signature(parsed, master_key=inputs["master_key"])  # type: ignore[arg-type]


@pytest.mark.unit
def test_parse_rejects_missing_required_key() -> None:
    raw = json.dumps({"manifest_version": 1, "vault_id": "x"}).encode()
    with pytest.raises(sync_manifest.InvalidManifestError, match="missing required keys"):
        sync_manifest.parse_manifest(raw)


@pytest.mark.unit
def test_parse_rejects_unknown_key() -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=1,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    polluted = json.loads(signed.to_json_bytes())
    polluted["next_run"] = "tomorrow"
    with pytest.raises(sync_manifest.InvalidManifestError, match="unknown keys"):
        sync_manifest.parse_manifest(json.dumps(polluted).encode())


@pytest.mark.unit
def test_parse_rejects_unsupported_version() -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=1,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    payload = json.loads(signed.to_json_bytes())
    payload["manifest_version"] = 999
    with pytest.raises(sync_manifest.InvalidManifestError, match="manifest_version"):
        sync_manifest.parse_manifest(json.dumps(payload).encode())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("vault_id", "01OTHERVAULTIDXXX2345678ZZ"),
        ("sequence_number", 2),
        ("schema_version", 5),
        ("audit_chain_head", "f" * 64),
        ("blob_id", "9" * 64),
        ("blob_nonce_b64", base64.b64encode(b"\x00" * 12).decode()),
        ("wrapped_dek_nonce_b64", base64.b64encode(b"\x11" * 12).decode()),
        ("wrapped_dek_b64", base64.b64encode(b"\x22" * 48).decode()),
        ("pushed_at_epoch", 9_999_999_999),
        ("manifest_version", 1),  # same value but re-canonicalises identically
    ],
)
def test_signature_detects_field_tampering(field: str, new_value: object) -> None:
    """Modifying ANY signed field invalidates the signature.

    Parametrises across every field the signing payload covers
    so a future regression that drops a field from
    ``_signing_payload`` (e.g., refactor that uses an explicit
    allowlist and forgets ``blob_nonce_b64``) is caught — the
    test for the dropped field would still pass under the
    weakened signature, but the OTHER tampered-field tests would
    keep firing and the new test for the surviving field would
    fail because the signature still binds it. The
    ``manifest_version`` case is the regression guard: writing
    the same value re-canonicalises identically and signature
    verification must still succeed (proves the test's
    re-encode round-trip is faithful).
    """

    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=1,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    payload = json.loads(signed.to_json_bytes())
    original = payload[field]
    payload[field] = new_value
    tampered_raw = json.dumps(payload).encode()
    parsed = sync_manifest.parse_manifest(tampered_raw)
    if new_value == original:
        # Identical-value rewrite: round-trip must still verify.
        sync_manifest.verify_signature(parsed, master_key=inputs["master_key"])  # type: ignore[arg-type]
    else:
        with pytest.raises(sync_manifest.InvalidSignatureError):
            sync_manifest.verify_signature(parsed, master_key=inputs["master_key"])  # type: ignore[arg-type]


@pytest.mark.unit
def test_empty_chain_sentinel_is_distinguished() -> None:
    """``EMPTY_CHAIN_SENTINEL`` must not collide with any real
    audit-chain row hash. Real hashes are 64-char lowercase hex;
    the sentinel contains a colon and is shorter, so a future
    caller doing ``if x:`` truthy check or ``len(x) == 64``
    shape check will not mistake it for a hash."""

    sentinel = sync_manifest.EMPTY_CHAIN_SENTINEL
    assert sentinel != ""
    assert ":" in sentinel
    assert len(sentinel) != 64
    # Cannot be a valid hex string:
    assert not all(c in "0123456789abcdef" for c in sentinel)


@pytest.mark.unit
def test_check_sequence_monotonic_passes_on_strictly_greater() -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=5,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    sync_manifest.check_sequence_monotonic(incoming=signed, last_restored_sequence=4)


@pytest.mark.unit
@pytest.mark.parametrize(("incoming_seq", "watermark"), [(5, 5), (5, 6), (5, 100)])
def test_check_sequence_monotonic_rejects_replay(incoming_seq: int, watermark: int) -> None:
    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=incoming_seq,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    with pytest.raises(sync_manifest.ReplayedManifestError, match="refusing"):
        sync_manifest.check_sequence_monotonic(incoming=signed, last_restored_sequence=watermark)


@pytest.mark.unit
def test_build_rejects_zero_sequence() -> None:
    inputs = _canonical_inputs()
    with pytest.raises(sync_manifest.InvalidManifestError, match="sequence_number"):
        sync_manifest.build_manifest(
            sequence_number=0,
            master_key=inputs["master_key"],  # type: ignore[arg-type]
            vault_id=inputs["vault_id"],  # type: ignore[arg-type]
            schema_version=inputs["schema_version"],  # type: ignore[arg-type]
            audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
            blob_id=inputs["blob_id"],  # type: ignore[arg-type]
            blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
            wrapped=inputs["wrapped"],  # type: ignore[arg-type]
            pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_signing_payload_excludes_signature() -> None:
    """The signature is computed against the manifest minus the
    signature field. A regression that included the signature
    field in its own input would change the byte sequence and
    break verification — pinning this defends against
    reorder-bug regressions."""

    inputs = _canonical_inputs()
    signed = sync_manifest.build_manifest(
        sequence_number=1,
        master_key=inputs["master_key"],  # type: ignore[arg-type]
        vault_id=inputs["vault_id"],  # type: ignore[arg-type]
        schema_version=inputs["schema_version"],  # type: ignore[arg-type]
        audit_chain_head=inputs["audit_chain_head"],  # type: ignore[arg-type]
        blob_id=inputs["blob_id"],  # type: ignore[arg-type]
        blob_nonce=inputs["blob_nonce"],  # type: ignore[arg-type]
        wrapped=inputs["wrapped"],  # type: ignore[arg-type]
        pushed_at_epoch=inputs["pushed_at_epoch"],  # type: ignore[arg-type]
    )
    payload = signed._signing_payload()
    decoded = json.loads(payload)
    assert "signature_b64" not in decoded
    assert decoded["sequence_number"] == 1
    # signature_b64 itself is non-empty base64.
    raw_sig = base64.b64decode(signed.signature_b64)
    assert len(raw_sig) == 32  # SHA-256 output length
