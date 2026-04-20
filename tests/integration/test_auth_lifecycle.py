"""Capability lifecycle and security guarantees against a real vault.

Covers:

* Full issue → use → refresh → revoke → reject flow (ADR 0007).
* Refresh-token strict one-time-use — replay of a rotated refresh token
  fails even though the original pair was valid seconds earlier.
* Revocation-within-30s — a revoked access token is rejected on the next
  verify call, no grace window.
* Audit trail — every lifecycle event lands in ``audit_log`` with the
  allowlisted payload shape and no raw secrets.
"""

from __future__ import annotations

import json

import pytest

from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.vault.connection import VaultConnection


def _new_agent(conn: object, *, external_id: str = "01AUTH") -> int:
    cur = conn.connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 0)",
        (external_id, "auth-test"),
    )
    rowid = cur.lastrowid
    assert rowid is not None
    return int(rowid)


@pytest.mark.integration
def test_issue_returns_well_formed_pair(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    scope = build_scope(read=["style", "episodic"], write=["style"])
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="claude-desktop",
        token_class="session",
        scope=scope,
        now_epoch=1_000_000,
    )
    assert issued.raw_token.startswith("tessera_session_")
    assert issued.raw_refresh_token is not None
    assert issued.raw_refresh_token.startswith("tessera_session_")
    # The refresh token is a distinct secret — different body bytes.
    assert issued.raw_token != issued.raw_refresh_token
    assert issued.expires_at == 1_000_000 + 30 * 60
    assert issued.refresh_expires_at == 1_000_000 + 7 * 24 * 60 * 60


@pytest.mark.integration
def test_subagent_has_no_refresh_pair(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="spawned-worker",
        token_class="subagent",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    assert issued.raw_refresh_token is None
    assert issued.refresh_expires_at is None


@pytest.mark.integration
def test_verify_resolves_issued_token(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=["style"]),
        now_epoch=1_000_000,
    )
    verified = tokens.verify_and_touch(
        open_vault.connection,
        raw_token=issued.raw_token,
        now_epoch=1_000_001,
    )
    assert verified.token_id == issued.token_id
    assert verified.agent_id == agent_id
    assert verified.client_name == "cli"
    assert verified.token_class == "session"
    assert verified.scope.allows(op="read", facet_type="style")


@pytest.mark.integration
def test_verify_updates_last_used_at(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    tokens.verify_and_touch(open_vault.connection, raw_token=issued.raw_token, now_epoch=1_000_123)
    row = open_vault.connection.execute(
        "SELECT last_used_at FROM capabilities WHERE id = ?", (issued.token_id,)
    ).fetchone()
    assert row[0] == 1_000_123


@pytest.mark.integration
def test_verify_rejects_unknown_token(open_vault: VaultConnection) -> None:
    _new_agent(open_vault)
    with pytest.raises(tokens.AuthDenied) as exc:
        tokens.verify_and_touch(
            open_vault.connection,
            raw_token="tessera_session_AAAAAAAAAAAAAAAAAAAAAAAA",
            now_epoch=1_000_000,
        )
    assert exc.value.reason == "unknown_token"


@pytest.mark.integration
def test_verify_rejects_malformed_token(open_vault: VaultConnection) -> None:
    _new_agent(open_vault)
    with pytest.raises(tokens.AuthDenied) as exc:
        tokens.verify_and_touch(open_vault.connection, raw_token="not-a-token", now_epoch=1_000_000)
    assert exc.value.reason == "malformed_token"


@pytest.mark.integration
def test_verify_rejects_expired_token(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="subagent",  # 15 min TTL
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    # One second past expiry.
    with pytest.raises(tokens.AuthDenied) as exc:
        tokens.verify_and_touch(
            open_vault.connection,
            raw_token=issued.raw_token,
            now_epoch=1_000_000 + 15 * 60 + 1,
        )
    assert exc.value.reason == "expired_token"


@pytest.mark.integration
def test_revoke_takes_effect_on_next_verify(open_vault: VaultConnection) -> None:
    """The ADR 0007 §Revocation guarantee: revocation reflects without a cache window.

    No code path in this module retains validity state across calls, so
    the next ``verify_and_touch`` after ``revoke`` always rejects. The
    30-second ceiling is a constraint on daemon-side caches built atop
    this API, not on the API itself.
    """

    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="service",
        scope=build_scope(read=["style"], write=["style"]),
        now_epoch=1_000_000,
    )
    # Works before revoke.
    tokens.verify_and_touch(open_vault.connection, raw_token=issued.raw_token, now_epoch=1_000_001)
    changed = tokens.revoke(
        open_vault.connection,
        token_id=issued.token_id,
        now_epoch=1_000_002,
        reason="operator_request",
    )
    assert changed is True
    with pytest.raises(tokens.AuthDenied) as exc:
        tokens.verify_and_touch(
            open_vault.connection, raw_token=issued.raw_token, now_epoch=1_000_003
        )
    assert exc.value.reason == "revoked_token"


@pytest.mark.integration
def test_revoke_twice_is_noop(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    assert (
        tokens.revoke(
            open_vault.connection,
            token_id=issued.token_id,
            now_epoch=1_000_001,
            reason="first",
        )
        is True
    )
    assert (
        tokens.revoke(
            open_vault.connection,
            token_id=issued.token_id,
            now_epoch=1_000_002,
            reason="second",
        )
        is False
    )


@pytest.mark.integration
def test_refresh_issues_new_pair_and_invalidates_old_access(
    open_vault: VaultConnection,
) -> None:
    agent_id = _new_agent(open_vault)
    original = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=["style"]),
        now_epoch=1_000_000,
    )
    assert original.raw_refresh_token is not None
    new_pair = tokens.refresh(
        open_vault.connection,
        raw_refresh_token=original.raw_refresh_token,
        now_epoch=1_000_100,
    )
    assert new_pair.token_id != original.token_id
    assert new_pair.raw_token != original.raw_token
    assert new_pair.raw_refresh_token != original.raw_refresh_token
    # Old access token must no longer verify.
    with pytest.raises(tokens.AuthDenied) as exc:
        tokens.verify_and_touch(
            open_vault.connection,
            raw_token=original.raw_token,
            now_epoch=1_000_101,
        )
    assert exc.value.reason == "revoked_token"
    # New pair verifies cleanly.
    verified = tokens.verify_and_touch(
        open_vault.connection, raw_token=new_pair.raw_token, now_epoch=1_000_101
    )
    assert verified.token_id == new_pair.token_id
    assert verified.scope.allows(op="write", facet_type="style")


@pytest.mark.integration
def test_refresh_is_strictly_one_time_use(open_vault: VaultConnection) -> None:
    """A replayed refresh token after rotation fails with AuthDenied.

    This is the stolen-token mitigation from ADR 0007: if the legitimate
    client has already refreshed, an attacker holding the old refresh
    token finds its hash gone from the row and cannot rotate again.
    """

    agent_id = _new_agent(open_vault)
    original = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    assert original.raw_refresh_token is not None
    tokens.refresh(
        open_vault.connection,
        raw_refresh_token=original.raw_refresh_token,
        now_epoch=1_000_100,
    )
    with pytest.raises(tokens.AuthDenied) as exc:
        tokens.refresh(
            open_vault.connection,
            raw_refresh_token=original.raw_refresh_token,
            now_epoch=1_000_200,
        )
    assert exc.value.reason == "unknown_refresh"


@pytest.mark.integration
def test_subagent_refresh_rejected(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="sub",
        token_class="subagent",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    # Subagent has no refresh token, but an attacker could try to use
    # the access token's raw value in refresh() — the shape still
    # matches the regex. The class-guard rejects it before the DB scan.
    with pytest.raises(tokens.RefreshNotSupportedError):
        tokens.refresh(
            open_vault.connection,
            raw_refresh_token=issued.raw_token,
            now_epoch=1_000_001,
        )


@pytest.mark.integration
def test_refresh_after_refresh_expiry_requires_reauth(
    open_vault: VaultConnection,
) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    assert issued.raw_refresh_token is not None
    # One second past the 7-day refresh window.
    with pytest.raises(tokens.ReauthRequired):
        tokens.refresh(
            open_vault.connection,
            raw_refresh_token=issued.raw_refresh_token,
            now_epoch=1_000_000 + 7 * 24 * 60 * 60 + 1,
        )


@pytest.mark.integration
def test_audit_trail_records_lifecycle_without_raw_secrets(
    open_vault: VaultConnection,
) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    assert issued.raw_refresh_token is not None
    tokens.refresh(
        open_vault.connection,
        raw_refresh_token=issued.raw_refresh_token,
        now_epoch=1_000_100,
    )
    tokens.revoke(
        open_vault.connection,
        token_id=issued.token_id + 1,  # the refreshed pair id
        now_epoch=1_000_200,
        reason="integration_check",
    )
    rows = open_vault.connection.execute(
        "SELECT op, payload FROM audit_log WHERE op IN ('token_issued','token_refreshed','token_revoked') ORDER BY id"
    ).fetchall()
    ops = [r[0] for r in rows]
    assert ops == [
        "token_issued",  # original
        "token_issued",  # new pair written by issue() called from refresh()
        "token_refreshed",
        "token_revoked",
    ]
    # §S4 no-content: no payload contains the raw token or the raw
    # refresh token.
    combined = " ".join(r[1] for r in rows)
    assert issued.raw_token not in combined
    assert issued.raw_refresh_token not in combined


@pytest.mark.integration
def test_auth_denied_emits_audit_row_without_leaking_input(
    open_vault: VaultConnection,
) -> None:
    _new_agent(open_vault)
    adversarial = "tessera_session_DEADBEEFDEADBEEFDEADBEEF"
    with pytest.raises(tokens.AuthDenied):
        tokens.verify_and_touch(open_vault.connection, raw_token=adversarial, now_epoch=1_000_000)
    row = open_vault.connection.execute(
        "SELECT payload FROM audit_log WHERE op = 'auth_denied'"
    ).fetchone()
    payload = json.loads(row[0])
    # Raw token value must not appear anywhere in the audit entry.
    assert adversarial not in row[0]
    assert payload["reason"] == "unknown_token"
    assert payload["client_name"] == "unknown"


@pytest.mark.integration
def test_record_scope_denial_writes_audit_row(open_vault: VaultConnection) -> None:
    agent_id = _new_agent(open_vault)
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )
    tokens.record_scope_denial(
        open_vault.connection,
        token_id=issued.token_id,
        client_name="cli",
        required_op="write",
        required_facet_type="episodic",
        now_epoch=1_000_005,
    )
    row = open_vault.connection.execute(
        "SELECT payload FROM audit_log WHERE op = 'scope_denied'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["token_id"] == issued.token_id
    assert payload["required_op"] == "write"
    assert payload["required_facet_type"] == "episodic"
