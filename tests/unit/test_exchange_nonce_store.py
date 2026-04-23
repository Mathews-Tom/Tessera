"""ChatGPT Dev Mode nonce-store invariants.

Security-critical: the store backs the one-time-use URL exchange that
hands ChatGPT its session token without the token ever living in a
URL. Every invariant below is a threat-model claim (ADR 0007 + the
``Token in URL`` row in ``docs/threat-model.md``) the tests exist to
pin.
"""

from __future__ import annotations

import pytest

from tessera.daemon.exchange import (
    NONCE_TTL_SECONDS,
    NonceStore,
    UnknownNonceError,
)


@pytest.mark.unit
def test_create_returns_distinct_nonces() -> None:
    store = NonceStore()
    a = store.create(raw_token="tok-a", now_epoch=1_000)
    b = store.create(raw_token="tok-b", now_epoch=1_000)
    assert a.nonce != b.nonce
    assert len(a.nonce) == 48  # 24 bytes hex


@pytest.mark.unit
def test_consume_returns_token_once() -> None:
    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=1_000)
    assert store.consume(nonce=entry.nonce, now_epoch=1_000) == "tok"
    # Second consume must fail — one-time use.
    with pytest.raises(UnknownNonceError):
        store.consume(nonce=entry.nonce, now_epoch=1_000)


@pytest.mark.unit
def test_consume_rejects_unknown_nonce() -> None:
    store = NonceStore()
    with pytest.raises(UnknownNonceError):
        store.consume(nonce="deadbeef" * 6, now_epoch=1_000)


@pytest.mark.unit
def test_consume_rejects_expired_nonce_with_same_error_shape() -> None:
    # Callers cannot distinguish "never issued" from "expired" from
    # "already consumed" — one error message across all three.
    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=1_000)
    with pytest.raises(UnknownNonceError, match="unknown or already consumed"):
        store.consume(nonce=entry.nonce, now_epoch=entry.expires_at + 1)


@pytest.mark.unit
def test_default_ttl_matches_spec() -> None:
    store = NonceStore()
    entry = store.create(raw_token="t", now_epoch=100)
    assert entry.expires_at == 100 + NONCE_TTL_SECONDS


@pytest.mark.unit
def test_consume_right_at_boundary_is_expired() -> None:
    # The cutoff is ``now >= expires_at``; a consume exactly at
    # ``expires_at`` must fail, closing the edge-case window.
    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=0)
    with pytest.raises(UnknownNonceError):
        store.consume(nonce=entry.nonce, now_epoch=entry.expires_at)


@pytest.mark.unit
def test_sweep_removes_expired_entries() -> None:
    store = NonceStore()
    a = store.create(raw_token="old", now_epoch=0)
    b = store.create(raw_token="new", now_epoch=100)
    removed = store.sweep(now_epoch=a.expires_at + 1)
    assert removed == 1
    assert store.pending_count() == 1
    # ``new`` is still redeemable.
    assert store.consume(nonce=b.nonce, now_epoch=100) == "new"


@pytest.mark.unit
def test_raw_token_gone_after_consume() -> None:
    """The store must not retain a reference to the token after consume."""

    store = NonceStore()
    entry = store.create(raw_token="super-secret", now_epoch=0)
    assert store.pending_count() == 1
    store.consume(nonce=entry.nonce, now_epoch=0)
    assert store.pending_count() == 0


@pytest.mark.unit
def test_random_bytes_injection_enables_deterministic_tests() -> None:
    # Injectable RNG so a suite can pin nonce values without monkeypatching
    # the secrets module.
    store = NonceStore()
    fixed = b"\xaa" * 24

    def fake(_n: int) -> bytes:
        return fixed

    entry = store.create(raw_token="tok", now_epoch=0, random_bytes=fake)
    assert entry.nonce == fixed.hex()
