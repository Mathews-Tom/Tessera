"""Token format and hash invariants at the unit layer.

The DB-backed lifecycle (issue → verify → refresh → revoke) lives in
``tests/integration/test_auth_lifecycle.py``; this module covers the
pure-Python parts so format or hashing regressions fail fast.
"""

from __future__ import annotations

import re

import pytest

from tessera.auth.tokens import (
    _ACCESS_TTL_SECONDS,
    _REFRESH_TTL_SECONDS,
    _TOKEN_FORMAT,
    _hash,
    _mint,
    _parse_raw_token,
)


@pytest.mark.unit
def test_mint_produces_well_formed_session_token() -> None:
    raw, token_hash, salt = _mint("session")
    assert _TOKEN_FORMAT.match(raw) is not None, raw
    assert raw.startswith("tessera_session_")
    assert len(token_hash) == 64  # sha256 hex
    assert len(salt) == 32  # 16 bytes hex


@pytest.mark.unit
def test_mint_entropy_is_non_repeating() -> None:
    # 100 mints → 100 distinct raw values. 120 bits of entropy makes
    # collision astronomically unlikely; a repeat indicates a broken RNG.
    raws = {_mint("service")[0] for _ in range(100)}
    assert len(raws) == 100


@pytest.mark.unit
def test_mint_hash_uses_the_stored_salt() -> None:
    raw, token_hash, salt = _mint("subagent")
    assert _hash(raw, salt) == token_hash
    # Different salt → different hash of the same raw.
    assert _hash(raw, "00" * 16) != token_hash


@pytest.mark.unit
def test_parse_raw_token_accepts_each_class() -> None:
    for cls in ("session", "service", "subagent"):
        raw, _, _ = _mint(cls)  # type: ignore[arg-type]
        parsed = _parse_raw_token(raw)
        assert parsed is not None
        assert parsed[0] == cls


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "tessera_session_tooshort",
        "tessera_SESSION_ABCDEFGHIJKLMNOPQRSTUVWX",  # class must be lowercase
        "tessera_unknown_ABCDEFGHIJKLMNOPQRSTUVWX",
        "tessra_session_ABCDEFGHIJKLMNOPQRSTUVWX",
        "ABCDEFGHIJKLMNOPQRSTUVWX",
        "",
        "tessera_session_abcdefghijklmnopqrstuvwx",  # lowercase body is invalid (base32 upper)
    ],
)
def test_parse_raw_token_rejects_bad_shapes(bad: str) -> None:
    assert _parse_raw_token(bad) is None


@pytest.mark.unit
def test_token_body_is_uppercase_base32_alphabet() -> None:
    raw, _, _ = _mint("session")
    body = raw.split("_")[-1]
    assert re.fullmatch(r"[A-Z2-7]{24}", body) is not None


@pytest.mark.unit
def test_ttls_match_adr_0007() -> None:
    assert _ACCESS_TTL_SECONDS["session"] == 30 * 60
    assert _ACCESS_TTL_SECONDS["service"] == 24 * 60 * 60
    assert _ACCESS_TTL_SECONDS["subagent"] == 15 * 60
    # Subagent has no refresh pair per ADR 0007 §Decision.
    assert "subagent" not in _REFRESH_TTL_SECONDS
    assert _REFRESH_TTL_SECONDS["session"] == 7 * 24 * 60 * 60
    assert _REFRESH_TTL_SECONDS["service"] == 7 * 24 * 60 * 60
