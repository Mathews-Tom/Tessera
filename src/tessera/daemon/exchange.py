"""ChatGPT Developer Mode bootstrap-nonce store.

ChatGPT Dev Mode cannot paste a long-lived bearer token into its MCP
configuration UI directly — the token lands in a URL (see
``docs/threat-model.md §Token in URL``), and URLs leak to browser
history, referrers, and proxy logs. The v0.1 remediation per ADR 0007
is a one-time-use nonce exchange: the CLI mints a short session token
on the daemon, stashes the *raw* token under a random nonce with a
30-second TTL and a single-use flag, and prints the nonce in a
bootstrap URL. ChatGPT POSTs the URL once; the daemon pops the
nonce entry and returns the session token in the JSON body. The raw
token never travels through the URL.

The store is purely in-memory: the capability-token row is already
persisted in the vault via :func:`tessera.auth.tokens.issue`, and the
nonce only gates the first transport. A daemon restart wipes pending
nonces, which is the desired failure mode — the bootstrap URL is a
30-second window, not a resumable session.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

# 30 seconds per ADR 0007 — long enough for a user to paste the URL
# into ChatGPT, short enough that a URL that leaks to a browser
# history entry or referrer log is worthless by the time anyone
# recovers it.
NONCE_TTL_SECONDS = 30

# 24 bytes → 48 hex chars → ~192 bits of entropy. Meaningfully above
# the 128-bit floor in ``docs/security-standards.md §Session rules`` so
# an attacker cannot brute-force the nonce within its 30-second window.
_NONCE_BYTES = 24


class ExchangeError(Exception):
    """Base class for exchange-flow failures."""


class UnknownNonceError(ExchangeError):
    """Nonce was never issued, has expired, or has already been consumed."""


@dataclass(frozen=True, slots=True)
class ExchangeEntry:
    """One pending bootstrap handshake."""

    nonce: str
    raw_token: str
    expires_at: int


@dataclass
class NonceStore:
    """Thread-safe, in-memory registry of pending nonces.

    The store owns the raw session-token string only between
    ``create`` and ``consume``. Once consumed, the entry is popped
    and the only remaining handle on the token is whatever the
    caller (ChatGPT) chooses to do with it. Once expired, the entry
    is popped on the next ``sweep`` call.
    """

    _entries: dict[str, ExchangeEntry] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def create(
        self,
        *,
        raw_token: str,
        now_epoch: int,
        ttl_seconds: int = NONCE_TTL_SECONDS,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> ExchangeEntry:
        """Mint a nonce, stash the token, return the entry.

        ``random_bytes`` is injected so tests can assert determinism
        without monkeypatching the ``secrets`` module globally.
        """

        nonce = random_bytes(_NONCE_BYTES).hex()
        entry = ExchangeEntry(
            nonce=nonce,
            raw_token=raw_token,
            expires_at=now_epoch + ttl_seconds,
        )
        with self._lock:
            # Collisions are astronomically improbable with 192-bit
            # nonces, but explicit is better than implicit: overwrite
            # would leak the prior entry's token by giving the same
            # nonce to two different pairings.
            if nonce in self._entries:  # pragma: no cover — cryptographic impossibility
                raise ExchangeError("nonce collision; retry")
            self._entries[nonce] = entry
        return entry

    def consume(self, *, nonce: str, now_epoch: int) -> str:
        """Validate and pop a nonce; return the paired raw token.

        Raises :class:`UnknownNonceError` when the nonce isn't in the
        store (never issued, already consumed, or expired and swept).
        Raises :class:`UnknownNonceError` when the nonce is present
        but its TTL has elapsed — callers cannot distinguish the two
        cases from the outside, by design (the error message is
        identical so a timing or content side-channel cannot reveal
        which state the store is in).
        """

        with self._lock:
            entry = self._entries.pop(nonce, None)
        if entry is None:
            raise UnknownNonceError("nonce is unknown or already consumed")
        if now_epoch >= entry.expires_at:
            raise UnknownNonceError("nonce is unknown or already consumed")
        return entry.raw_token

    def sweep(self, *, now_epoch: int) -> int:
        """Discard expired entries; return the count removed."""

        with self._lock:
            expired = [
                nonce for nonce, entry in self._entries.items() if now_epoch >= entry.expires_at
            ]
            for nonce in expired:
                self._entries.pop(nonce, None)
        return len(expired)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "NONCE_TTL_SECONDS",
    "ExchangeEntry",
    "ExchangeError",
    "NonceStore",
    "UnknownNonceError",
]
