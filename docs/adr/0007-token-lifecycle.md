# ADR 0007 — Token lifecycle: short TTL, refresh, Unix-socket default

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

Tessera's v0.1 draft design described capability tokens as scoped bearer strings, hashed at rest, revocable by CLI. Missing from that description: expiry, refresh, transport defaults, binding, revocation propagation, and guidance on ChatGPT Developer Mode URL-token transport.

Threat model (docs/threat-model.md §S2) enumerates the risks:

- Token theft from client-config files (`~/.claude/claude_desktop_config.json`, `.codex/config.toml`).
- Replay of captured HTTP MCP requests.
- Token in URL: leaks to browser history, referrer headers, server logs.
- Revocation lag: a compromised token that is "revoked" remains usable for however long the daemon caches validity.
- Hash weakness: SHA256 of a short base32 token is dictionary-enumerable.

## Decision

Adopt the following posture, implemented in v0.1 unless noted:

1. **Mandatory expiry.** Every token has an `expires_at` in `capabilities`. No indefinite tokens.
2. **Three token classes** — `session` (30 min TTL), `service` (24 h TTL), `subagent` (15 min TTL).
3. **Refresh-token pattern for `session` and `service`.** Paired one-time-use refresh tokens rotate on every use.
4. **Per-row salt** in token hashing: `sha256(salt || token)`. Salt stored alongside the hash in `capabilities`.
5. **Unix socket is the default control-plane transport.** HTTP MCP is agent-facing and requires the token header; Unix socket authenticates via filesystem permission (mode 0600) and is used by the CLI.
6. **URL-embedded token transport is deprecated**; ChatGPT Developer Mode connects via an exchange endpoint that issues a session token to a short-lived localhost URL.
7. **Revocation reflects within 30 s.** No in-memory token-validity cache lives longer than 30 s anywhere in the daemon.
8. **Binding (UID, client fingerprint) is deferred to v0.3** as an opt-in hardening layer. Mandatory binding for `service` tokens is a v1.0 consideration.

## Rationale

1. **Bearer tokens without expiry are equivalent to passwords with infinite lifetime.** Short TTL caps the value of a single exfiltration. Refresh-token rotation means a stolen bearer expires within 30 minutes even if the refresh token was also stolen — the legitimate client will rotate it, invalidating the attacker's copy.
2. **Three classes balance usability and safety.** Interactive agents (Claude Desktop, Cursor) fit `session`; autonomous agents with no human-in-the-loop fit `service`; spawned subagents fit `subagent`. Collapsing to one class would force `service` ergonomics onto `session` contexts or vice versa.
3. **Unix-socket default is correct for the actual workload.** The CLI is the primary control-plane client and runs as the same OS user. HTTP MCP exists because agent runtimes prefer it; it does not need to be the transport for privileged operations.
4. **URL-token transport cannot be made safe.** Browser history, referrer headers, server-side access logs, OS clipboard history, and screen-capture tools all see URLs. The exchange endpoint shifts URL exposure to a one-time, short-lived token rather than the long-lived capability token.
5. **Revocation-within-30-seconds is the useful guarantee.** A stolen-token response that triggers in hours has limited forensic and remediation value; within a minute, the operator has a realistic window to contain.
6. **Binding is opt-in in v0.3, not v0.1.** UID/fingerprint binding requires transport metadata the v0.1 MCP surface does not uniformly carry. Shipping it in v0.1 would force non-portable assumptions; shipping it in v0.3 lets it evolve with the MCP ecosystem.

## Consequences

**Positive:**
- Stolen-token window is capped by TTL, not by the user remembering to revoke.
- Forensics improve: the audit log shows token rotations, making anomalies (unexpected rotation, unexpected issuance) visible.
- CLI operations do not traverse any network socket.

**Negative:**
- Token refresh is a new failure mode. Agents must handle 401 + token-revoked responses and transparently refresh. MCP client libraries may not all handle this cleanly; Tessera-side adapter code helps.
- Per-row salting adds a small schema cost. Worth it.
- ChatGPT Dev Mode integration is more complex than a config-file paste. Documented as a supported but not-recommended connector.

## Schema implications

```sql
ALTER TABLE capabilities
  ADD COLUMN salt BLOB NOT NULL DEFAULT (randomblob(16)),
  ADD COLUMN token_class TEXT NOT NULL DEFAULT 'session'
    CHECK (token_class IN ('session', 'service', 'subagent')),
  ADD COLUMN expires_at INTEGER NOT NULL,
  ADD COLUMN refresh_token_hash TEXT,
  ADD COLUMN refresh_expires_at INTEGER,
  ADD COLUMN ui_fingerprint TEXT;  -- v0.3+, nullable

CREATE INDEX capabilities_expires ON capabilities(expires_at)
  WHERE revoked_at IS NULL;
```

## Alternatives considered

- **Long-lived bearer tokens only.** Simplest; weakest posture. Rejected.
- **mTLS for every agent.** Strongest; forces certificate management on every MCP client. Ecosystem does not support this uniformly. Rejected for v0.1; may revisit for v1.0.
- **OAuth device-code flow.** Complex for a single-machine local daemon. Not justified by threat model. Rejected.
- **No HTTP transport, Unix-socket only.** Would break ChatGPT Dev Mode and any remote-agent scenario. Rejected.

## Revisit triggers

- MCP spec adopts a richer auth negotiation; migrate to it if better than bespoke.
- A published vulnerability in sha256-salted short-token storage; rotate scheme.
- Real-world evidence of token exfiltration events in Tessera deployments; consider mandatory binding.
