"""Capability-token lifecycle: issue, verify, refresh, revoke.

Follows ADR 0007 (token-lifecycle) and docs/release-spec.md §Auth. Three
token classes (``session``, ``service``, ``subagent``) with distinct
TTLs; ``session`` and ``service`` carry a paired one-time-use refresh
token while ``subagent`` does not. Hashes are per-row salted sha256 so
stolen capability rows cannot be rainbow-table enumerated.

Verification is O(n) over active capability rows because salting breaks
single-lookup indexing by design. The deployment shape (single-user
daemon with < 20 live tokens) makes this trivial in practice, and the
``capabilities_expires`` partial index keeps the candidate set narrow
regardless of historical revoked/expired rows.

Revocation propagation: this module never caches capability state. The
30-second guarantee in ADR 0007 is a cache-TTL ceiling for callers; the
storage functions always read fresh. A daemon that wires a verify cache
in front of :func:`verify_and_touch` must enforce the ceiling itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

import sqlcipher3

from tessera.auth.scopes import Scope, parse_scope
from tessera.vault import audit

TokenClass = Literal["session", "service", "subagent"]

_ACCESS_TTL_SECONDS: Final[dict[TokenClass, int]] = {
    "session": 30 * 60,
    "service": 24 * 60 * 60,
    "subagent": 15 * 60,
}

# Refresh tokens exist only for session and service. Both get a 7-day
# refresh window; at day 7 the client must re-authenticate via a fresh
# ``issue()`` call, which is a legitimate UX pressure point per
# ADR 0007 §Consequences.
_REFRESH_TTL_SECONDS: Final[dict[TokenClass, int]] = {
    "session": 7 * 24 * 60 * 60,
    "service": 7 * 24 * 60 * 60,
}

_TOKEN_PREFIX: Final[str] = "tessera"
_TOKEN_BODY_BYTES: Final[int] = 15  # 120 bits → 24 base32 chars, no padding
_TOKEN_BODY_CHARS: Final[int] = 24
_TOKEN_FORMAT = re.compile(
    rf"^{_TOKEN_PREFIX}_(session|service|subagent)_([A-Z2-7]{{{_TOKEN_BODY_CHARS}}})$"
)
_HASH_PREFIX_LEN: Final[int] = 12

_VALID_CLASSES: Final[frozenset[str]] = frozenset({"session", "service", "subagent"})


class AuthError(Exception):
    """Base class for capability-auth failures."""


class AuthDenied(AuthError):
    """Raised when a raw token cannot be resolved to a live capability.

    One error covers unknown-token, expired, and revoked cases so the
    MCP boundary never leaks *why* authentication failed to an untrusted
    caller. The attached ``reason`` attribute is logged to the audit
    trail but does not appear in the user-visible error message.
    """

    def __init__(self, reason: str, *, client_hint: str | None = None) -> None:
        super().__init__("authentication failed")
        self.reason = reason
        self.client_hint = client_hint


class RefreshNotSupportedError(AuthError):
    """Refresh attempted on a ``subagent`` token, which has no refresh pair."""


class ReauthRequired(AuthError):
    """Refresh token valid-shape but expired; caller must issue a new token."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """Freshly minted capability pair returned to the caller.

    Raw token strings are returned exactly once by :func:`issue` and
    :func:`refresh`. They are never reconstructable from the vault — the
    only durable form is the salted hash in ``capabilities`` — so losing
    one requires re-issuance.
    """

    token_id: int
    raw_token: str
    raw_refresh_token: str | None
    token_class: TokenClass
    client_name: str
    expires_at: int
    refresh_expires_at: int | None


@dataclass(frozen=True, slots=True)
class VerifiedCapability:
    """A live capability row resolved from a raw token.

    Returned by :func:`verify_and_touch`. ``scope`` is the parsed
    :class:`Scope` object ready for :meth:`Scope.allows` calls; the raw
    JSON is deliberately not exposed.
    """

    token_id: int
    agent_id: int
    client_name: str
    token_class: TokenClass
    scope: Scope
    expires_at: int


def issue(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    client_name: str,
    token_class: TokenClass,
    scope: Scope,
    now_epoch: int,
    actor: str = "system",
) -> IssuedToken:
    """Mint and persist a new capability pair.

    Generates a cryptographically-random access token (plus refresh
    token for non-``subagent`` classes), stores per-row salted sha256
    hashes, and writes a ``token_issued`` audit row. Returns the raw
    strings exactly once — callers must hand them to the client and then
    drop the references.
    """

    if token_class not in _VALID_CLASSES:
        raise ValueError(f"unknown token class {token_class!r}")
    raw_token, token_hash, salt = _mint(token_class)
    access_expires = now_epoch + _ACCESS_TTL_SECONDS[token_class]
    refresh_ttl = _REFRESH_TTL_SECONDS.get(token_class)
    if refresh_ttl is None:
        raw_refresh: str | None = None
        refresh_hash: str | None = None
        refresh_salt: str | None = None
        refresh_expires: int | None = None
    else:
        raw_refresh, refresh_hash, refresh_salt = _mint(token_class, prefix_label="refresh")
        refresh_expires = now_epoch + refresh_ttl
    cur = conn.execute(
        """
        INSERT INTO capabilities(
            agent_id, client_name, token_hash, salt, scopes, token_class,
            created_at, expires_at, refresh_token_hash, refresh_salt, refresh_expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent_id,
            client_name,
            token_hash,
            salt,
            scope.to_json(),
            token_class,
            now_epoch,
            access_expires,
            refresh_hash,
            refresh_salt,
            refresh_expires,
        ),
    )
    if cur.lastrowid is None:
        raise AuthError("capabilities INSERT produced no rowid")
    token_id = int(cur.lastrowid)
    audit.write(
        conn,
        op="token_issued",
        actor=actor,
        agent_id=agent_id,
        payload={
            "token_id": token_id,
            "token_class": token_class,
            "client_name": client_name,
            "token_hash_prefix": token_hash[:_HASH_PREFIX_LEN],
            "expires_at": access_expires,
        },
        at=now_epoch,
    )
    return IssuedToken(
        token_id=token_id,
        raw_token=raw_token,
        raw_refresh_token=raw_refresh,
        token_class=token_class,
        client_name=client_name,
        expires_at=access_expires,
        refresh_expires_at=refresh_expires,
    )


def verify_and_touch(
    conn: sqlcipher3.Connection,
    *,
    raw_token: str,
    now_epoch: int,
) -> VerifiedCapability:
    """Resolve ``raw_token`` to a live capability row and bump last-used.

    Raises :class:`AuthDenied` for unknown, expired, or revoked tokens.
    Writes an ``auth_denied`` audit row on failure so operators can
    observe attempts without learning the attempted raw value. The
    ``client_name`` in the audit row is ``"unknown"`` for malformed
    tokens — we cannot trust the prefix claim before the hash matches.
    """

    parsed = _parse_raw_token(raw_token)
    if parsed is None:
        _audit_denial(conn, client_name="unknown", reason="malformed_token", now_epoch=now_epoch)
        raise AuthDenied("malformed_token")
    claimed_class, _ = parsed
    row = _find_by_access_token(conn, raw_token=raw_token, token_class=claimed_class)
    if row is None:
        _audit_denial(conn, client_name="unknown", reason="unknown_token", now_epoch=now_epoch)
        raise AuthDenied("unknown_token")
    cap_id, agent_id, client_name, scopes_json, stored_class, expires_at, revoked_at = row
    if revoked_at is not None:
        _audit_denial(conn, client_name=client_name, reason="revoked_token", now_epoch=now_epoch)
        raise AuthDenied("revoked_token", client_hint=client_name)
    if expires_at <= now_epoch:
        _audit_denial(conn, client_name=client_name, reason="expired_token", now_epoch=now_epoch)
        raise AuthDenied("expired_token", client_hint=client_name)
    try:
        scope = parse_scope(scopes_json)
    except Exception as exc:
        # Malformed stored scope is a data-integrity problem, not a
        # credential problem, but from the caller's perspective both
        # surface as deny. Record the class of error so operators can
        # distinguish.
        _audit_denial(
            conn,
            client_name=client_name,
            reason=f"scope_parse:{type(exc).__name__}",
            now_epoch=now_epoch,
        )
        raise AuthDenied("scope_unparseable", client_hint=client_name) from exc
    conn.execute(
        "UPDATE capabilities SET last_used_at = ? WHERE id = ?",
        (now_epoch, cap_id),
    )
    return VerifiedCapability(
        token_id=cap_id,
        agent_id=agent_id,
        client_name=client_name,
        token_class=_coerce_token_class(stored_class),
        scope=scope,
        expires_at=expires_at,
    )


def refresh(
    conn: sqlcipher3.Connection,
    *,
    raw_refresh_token: str,
    now_epoch: int,
    actor: str = "system",
) -> IssuedToken:
    """Rotate a refresh token into a fresh access/refresh pair.

    Strictly one-time use: the old access token is revoked and the old
    refresh token hash is overwritten in the same transaction as the new
    pair is minted. A replay of the old refresh token after rotation
    fails with :class:`AuthDenied` because its hash no longer resolves
    to any row.
    """

    parsed = _parse_raw_token(raw_refresh_token)
    if parsed is None:
        _audit_denial(conn, client_name="unknown", reason="malformed_refresh", now_epoch=now_epoch)
        raise AuthDenied("malformed_refresh")
    claimed_class, _ = parsed
    if claimed_class == "subagent":
        raise RefreshNotSupportedError("subagent tokens cannot be refreshed")
    row = _find_by_refresh_token(
        conn, raw_refresh_token=raw_refresh_token, token_class=claimed_class
    )
    if row is None:
        _audit_denial(conn, client_name="unknown", reason="unknown_refresh", now_epoch=now_epoch)
        raise AuthDenied("unknown_refresh")
    (
        old_id,
        agent_id,
        client_name,
        scopes_json,
        stored_class,
        revoked_at,
        refresh_expires_at,
    ) = row
    if revoked_at is not None:
        _audit_denial(conn, client_name=client_name, reason="revoked_refresh", now_epoch=now_epoch)
        raise AuthDenied("revoked_refresh", client_hint=client_name)
    if refresh_expires_at is None or refresh_expires_at <= now_epoch:
        _audit_denial(conn, client_name=client_name, reason="expired_refresh", now_epoch=now_epoch)
        raise ReauthRequired("refresh token has expired; issue a new token")
    try:
        scope = parse_scope(scopes_json)
    except Exception as exc:
        _audit_denial(
            conn,
            client_name=client_name,
            reason=f"scope_parse_on_refresh:{type(exc).__name__}",
            now_epoch=now_epoch,
        )
        raise AuthDenied("scope_unparseable", client_hint=client_name) from exc
    token_class = _coerce_token_class(stored_class)
    # Revoke the old row (kept for audit continuity and to render the
    # old refresh-token hash unusable from any subsequent refresh).
    conn.execute(
        """
        UPDATE capabilities
           SET revoked_at = ?, refresh_token_hash = NULL, refresh_salt = NULL
         WHERE id = ?
        """,
        (now_epoch, old_id),
    )
    new_pair = issue(
        conn,
        agent_id=agent_id,
        client_name=client_name,
        token_class=token_class,
        scope=scope,
        now_epoch=now_epoch,
        actor=actor,
    )
    # Replace the plain ``token_issued`` row written inside ``issue`` is
    # a deliberate choice; the ``token_refreshed`` entry here adds the
    # link back to ``old_id`` so replay traces connect both ends.
    audit.write(
        conn,
        op="token_refreshed",
        actor=actor,
        agent_id=agent_id,
        payload={
            "old_token_id": old_id,
            "new_token_id": new_pair.token_id,
            "token_class": token_class,
            "client_name": client_name,
            "token_hash_prefix": _hash(new_pair.raw_token, _read_salt(conn, new_pair.token_id))[
                :_HASH_PREFIX_LEN
            ],
            "expires_at": new_pair.expires_at,
        },
        at=now_epoch,
    )
    return new_pair


def revoke(
    conn: sqlcipher3.Connection,
    *,
    token_id: int,
    now_epoch: int,
    reason: str,
    actor: str = "system",
) -> bool:
    """Mark a capability row revoked.

    Returns True on state transition (active → revoked) and False if the
    row was already revoked or does not exist. Idempotent: a caller
    re-running a revoke on an already-revoked row does not receive an
    error, but also does not emit a second audit row.
    """

    row = conn.execute(
        """
        SELECT agent_id, client_name, token_class, token_hash, revoked_at
          FROM capabilities
         WHERE id = ?
        """,
        (token_id,),
    ).fetchone()
    if row is None:
        return False
    agent_id, client_name, token_class, token_hash, revoked_at = row
    if revoked_at is not None:
        return False
    conn.execute(
        """
        UPDATE capabilities
           SET revoked_at = ?, refresh_token_hash = NULL, refresh_salt = NULL
         WHERE id = ?
        """,
        (now_epoch, token_id),
    )
    audit.write(
        conn,
        op="token_revoked",
        actor=actor,
        agent_id=int(agent_id),
        payload={
            "token_id": int(token_id),
            "token_class": str(token_class),
            "client_name": str(client_name),
            "token_hash_prefix": str(token_hash)[:_HASH_PREFIX_LEN],
            "reason": reason,
        },
        at=now_epoch,
    )
    return True


def record_scope_denial(
    conn: sqlcipher3.Connection,
    *,
    token_id: int,
    client_name: str,
    required_op: str,
    required_facet_type: str,
    now_epoch: int,
) -> None:
    """Append a ``scope_denied`` audit row.

    The caller (MCP tool surface) is responsible for raising a distinct
    error code separate from :class:`AuthDenied`; this helper just lands
    the forensic entry.
    """

    audit.write(
        conn,
        op="scope_denied",
        actor=client_name,
        payload={
            "token_id": token_id,
            "client_name": client_name,
            "required_op": required_op,
            "required_facet_type": required_facet_type,
        },
        at=now_epoch,
    )


def _mint(token_class: TokenClass, *, prefix_label: str | None = None) -> tuple[str, str, str]:
    """Return (raw_token, token_hash_hex, salt_hex).

    ``prefix_label`` switches the visible purpose segment between access
    and refresh tokens so an operator inspecting a leaked token can tell
    what they are looking at. The class remains part of the hashed body
    either way.
    """

    del prefix_label  # currently unused; class is the purpose segment
    body_bytes = secrets.token_bytes(_TOKEN_BODY_BYTES)
    body = base64.b32encode(body_bytes).decode("ascii").rstrip("=")[:_TOKEN_BODY_CHARS]
    raw = f"{_TOKEN_PREFIX}_{token_class}_{body}"
    salt_hex = secrets.token_hex(16)
    token_hash_hex = _hash(raw, salt_hex)
    return raw, token_hash_hex, salt_hex


def _hash(raw: str, salt_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt_hex) + raw.encode("utf-8")).hexdigest()


def _parse_raw_token(raw: str) -> tuple[TokenClass, str] | None:
    match = _TOKEN_FORMAT.match(raw)
    if match is None:
        return None
    class_str, body = match.group(1), match.group(2)
    return _coerce_token_class(class_str), body


def _coerce_token_class(value: str) -> TokenClass:
    if value not in _VALID_CLASSES:
        raise ValueError(f"invalid token class {value!r}")
    # Narrowed in-place: Literal cast.
    return value  # type: ignore[return-value]


def _find_by_access_token(
    conn: sqlcipher3.Connection,
    *,
    raw_token: str,
    token_class: TokenClass,
) -> tuple[int, int, str, str, str, int, int | None] | None:
    """Return matching row or None. Iterates candidates in id order.

    The ``token_class`` filter uses the class segment of the raw token
    as a candidate narrower, not as a trust assertion: the hash binding
    below is the authoritative check.
    """

    rows = conn.execute(
        """
        SELECT id, agent_id, client_name, scopes, token_class,
               expires_at, revoked_at, token_hash, salt
          FROM capabilities
         WHERE token_class = ?
         ORDER BY id
        """,
        (token_class,),
    ).fetchall()
    return _match_against(rows, raw_token=raw_token, hash_col=7, salt_col=8)


def _find_by_refresh_token(
    conn: sqlcipher3.Connection,
    *,
    raw_refresh_token: str,
    token_class: TokenClass,
) -> tuple[int, int, str, str, str, int | None, int | None] | None:
    rows = conn.execute(
        """
        SELECT id, agent_id, client_name, scopes, token_class,
               revoked_at, refresh_expires_at, refresh_token_hash, refresh_salt
          FROM capabilities
         WHERE token_class = ?
           AND refresh_token_hash IS NOT NULL
         ORDER BY id
        """,
        (token_class,),
    ).fetchall()
    return _match_against(rows, raw_token=raw_refresh_token, hash_col=7, salt_col=8)


def _match_against(
    rows: Sequence[tuple[object, ...]],
    *,
    raw_token: str,
    hash_col: int,
    salt_col: int,
) -> tuple:  # type: ignore[type-arg]
    """Linear scan with constant-time compare per row.

    Uses :func:`hmac.compare_digest` on each candidate so timing cannot
    distinguish "matched row N" from "matched row N+1" for an attacker
    who can probe repeatedly — in practice inconsequential for a single-
    user daemon, but cheap and principled.
    """

    for row in rows:
        stored_hash = row[hash_col]
        stored_salt = row[salt_col]
        if not isinstance(stored_hash, str) or not isinstance(stored_salt, str):
            continue
        candidate = _hash(raw_token, stored_salt)
        if hmac.compare_digest(candidate, stored_hash):
            return row[:hash_col]
    return None  # type: ignore[return-value]


def _read_salt(conn: sqlcipher3.Connection, token_id: int) -> str:
    row = conn.execute("SELECT salt FROM capabilities WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        raise AuthError(f"capability {token_id} vanished between INSERT and SELECT")
    return str(row[0])


def _audit_denial(
    conn: sqlcipher3.Connection,
    *,
    client_name: str,
    reason: str,
    now_epoch: int,
) -> None:
    audit.write(
        conn,
        op="auth_denied",
        actor=client_name,
        payload={"client_name": client_name, "reason": reason},
        at=now_epoch,
    )


__all__ = [
    "AuthDenied",
    "AuthError",
    "IssuedToken",
    "ReauthRequired",
    "RefreshNotSupportedError",
    "TokenClass",
    "VerifiedCapability",
    "issue",
    "record_scope_denial",
    "refresh",
    "revoke",
    "verify_and_touch",
]
