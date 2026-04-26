# Tessera — Threat Model

**Status:** Draft 1
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## Scope

This document enumerates the security-relevant assets, threats, and mitigations in scope for Tessera v0.1 through v1.0. It is the authoritative source for security decisions. Features and code must be checked against this model; when a feature cannot be delivered without violating a mitigation, the threat model is re-opened, not the feature.

## Assets

| Asset                            | Description                                                                     | Sensitivity                                                                |
| -------------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| A1 — Vault file                  | `~/.tessera/vault.db`: facets, capabilities, audit log, vector indexes          | High. Contains the user's full portable context across identity, preferences, workflows, projects, and style. |
| A2 — Capability tokens (live)    | Raw tokens presented by MCP-connected AI tools in requests                      | High. Bearer; possession = tool impersonation across the token's granted facet-type scopes. |
| A3 — Capability tokens (at rest) | Hashed tokens in `capabilities.token_hash`                                      | Low on their own; high when correlated with A2.                            |
| A4 — Daemon process              | `tesserad` running, holding the vault open, serving MCP                         | Medium. Compromise = persistent access to A1, A2.                          |
| A5 — Config files                | `~/.tessera/config.yaml`, client-side MCP configs (Claude Desktop, Codex, etc.) | Medium. Client configs typically hold the raw token.                       |
| A6 — Model adapter credentials   | OpenAI / Voyage / Cohere API keys, when cloud adapters are opt-in               | High when present. Not present in all-local mode.                          |
| A7 — Audit log                   | Append-only record of operations                                                | Medium. Reveals activity patterns; compromise = deniability loss.          |
| A8 — Embeddings                  | Dense vectors of facet content                                                  | Low on their own; high via embedding inversion attacks at scale.           |

## Actors

| Actor                                   | Description                                                                      | Default trust                                           |
| --------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------- |
| T1 — User                               | The human on the machine                                                         | Trusted                                                 |
| T2 — Connected AI tool                  | Any MCP client (Claude Desktop, Claude Code, Cursor, Codex, ChatGPT Dev Mode, autonomous agent) running with a valid capability token | Trusted within the scopes granted to its token          |
| T3 — Co-located process                 | Any other process running as the same OS user                                    | **Untrusted**                                           |
| T4 — Co-located user (shared host)      | Another OS user on the same machine (SSH, multi-user dev box)                    | Untrusted                                               |
| T5 — Remote network attacker            | Attacker on the LAN or internet                                                  | Untrusted; must not be reachable by default             |
| T6 — Physical attacker with disk access | Attacker who can read the filesystem offline (stolen laptop, unencrypted backup) | Untrusted                                               |
| T7 — Cloud model provider               | OpenAI / Voyage / Cohere when opt-in adapters are used                           | Semi-trusted (they see queries + embeddings, not vault) |
| T8 — Sync backend (v0.5+)               | BYO S3-compatible storage                                                        | Untrusted (must only see ciphertext)                    |

## STRIDE by surface

### S1 — Vault file (A1)

| Threat                                             | Category               | Vector                                                                      | Mitigation                                                                                                                                                 | Status                               |
| -------------------------------------------------- | ---------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| Offline read of vault by attacker with disk access | Information disclosure | T6 reads `vault.db` from stolen disk, unencrypted backup, emailed copy      | **Encryption at rest**: sqlcipher or libsodium-encrypted pages, key derived via argon2id from user passphrase. See `system-design.md §Encryption at rest`. | v0.1 mandatory                       |
| Offline modification of vault                      | Tampering              | T6 edits `vault.db` to plant facets, rewrite audit log, insert capabilities | Encryption at rest makes modification require the key; HMAC-chained audit log makes tampering detectable.                                                  | v0.1 (encryption), v0.3 (HMAC chain) |
| Accidental exposure via user action                | Information disclosure | User emails vault, uploads to Dropbox unencrypted, commits to git           | Default encryption means the exported file is ciphertext. Docs explicitly warn against sharing the passphrase.                                             | v0.1                                 |
| Cross-tool scope leak                              | Information disclosure | A token granted `read: [preference, workflow]` to ChatGPT returns `style` facets to that tool | Per-facet-type scope check is the first gate of every `recall`/`show`/`list_facets`/`stats` path (see `system-design.md §Trust & capability tokens`). Integration tests cover every (scope-set, facet-type) combination. v1.0 multi-user isolation additionally enforces `user_id` foreign key on every SELECT. | v0.1 (scope check); v1.0 (multi-user)|

### S2 — Capability tokens (A2, A3)

| Threat                               | Category               | Vector                                                                      | Mitigation                                                                                                                | Status                          |
| ------------------------------------ | ---------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------- | -------- | ---- |
| Token theft from client config       | Information disclosure | T3 reads `~/.claude/claude_desktop_config.json`, `.codex/config.toml`       | Short TTL (15–60 min) + refresh token flow; token binding to client fingerprint. See ADR 0007 (token lifecycle, pending). | v0.1 (TTL), v0.3 (binding)      |
| Token replay from captured request   | Elevation of privilege | T3 observes HTTP MCP request, replays with same token before expiry         | Nonce + timestamp in authenticated request, rejected on replay within a sliding window.                                   | v0.3                            |
| Token in URL via ChatGPT Dev Mode    | Information disclosure | Token leaks to browser history, referrers, proxy logs                       | Docs explicitly warn; proxy/exchange endpoint that takes URL-token and issues short-lived bearer.                         | v0.1 (warning), v0.3 (exchange) |
| Revocation lag on in-flight sessions | Elevation of privilege | Compromised token revoked, but in-flight session continues until disconnect | Every MCP request re-validates token against `revoked_at`; no in-memory cache > 30s.                                      | v0.1                            |
| Hash weakness                        | Information disclosure | SHA256 of a short token is still enumerable for known-format tokens         | Add per-token random salt in `capabilities.token_hash`: `sha256(salt                                                      |                                 | token)`. | v0.1 |

### S3 — Daemon process (A4) and MCP transport

| Threat                                          | Category               | Vector                                                       | Mitigation                                                                                                                                                  | Status |
| ----------------------------------------------- | ---------------------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| Co-located process queries HTTP MCP on loopback | Elevation of privilege | T3 sends requests to `127.0.0.1:5710` on single-user machine | Unix socket is the default control-plane transport. HTTP MCP is opt-in per-client and warns when a multi-user login session is detected.                    | v0.1   |
| Co-located user on multi-user host              | Elevation of privilege | T4 connects to loopback port bound by another user           | Bind to `$XDG_RUNTIME_DIR/tessera/tessera.sock` (mode 0600) by default. HTTP MCP, if enabled, binds to a random high port recorded in a user-private file.  | v0.1   |
| Containerized peer on loopback                  | Elevation of privilege | A Docker container on host network calls `127.0.0.1:5710`    | Docs: "If running agents in containers, use mTLS over a dedicated port with pinned cert." Default configuration does not listen on non-loopback interfaces. | v0.3   |
| Daemon crash with in-memory secrets             | Information disclosure | Core dump captures unlock key / decrypted vault pages        | `PR_SET_DUMPABLE` on Linux; `setrlimit(RLIMIT_CORE, 0)` everywhere. mlock the unlock key.                                                                   | v0.3   |
| Arbitrary tool invocation via CSRF from browser | Elevation of privilege | Malicious page posts to `127.0.0.1:5710`                     | Require `Origin` header on all HTTP MCP requests; reject browsers by default (MCP clients send identifying user-agent).                                     | v0.1   |

### S4 — Audit log (A7)

| Threat                   | Category               | Vector                                                    | Mitigation                                                                                                     | Status |
| ------------------------ | ---------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------ | --------- | --- | -------------------------------------------- | ---- |
| Tamper of audit log rows | Repudiation            | Attacker with vault write access deletes or rewrites rows | HMAC-chained entries: each row includes `HMAC(k, prev_id                                                       |        | prev_hash |     | this_entry)`. Chain break = tamper evidence. | v0.3 |
| Audit log exhausts disk  | Denial of service      | Runaway agent writes audit entries without bound          | Rate-limit audit log writes per capability token; rotate to a cold archive table at configurable row count.    | v0.3   |
| Audit log PII leak       | Information disclosure | Log payloads contain secret content hashed into facets    | Payloads store IDs and operation metadata, not facet content. Explicit allow-list of keys in the payload JSON. | v0.1   |

### S5 — Model adapters (A6) and cloud providers (T7)

| Threat                                                | Category               | Vector                                                                | Mitigation                                                                                                                                                                   | Status |
| ----------------------------------------------------- | ---------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| API key in config file readable by co-located process | Information disclosure | T3 reads `config.yaml`                                                | OS keyring-backed secret storage (macOS Keychain, secret-service on Linux, Windows Credential Manager). Config stores a keyring reference, not the key.                      | v0.1   |
| API key in process env readable by co-located process | Information disclosure | T3 reads `/proc/<pid>/environ` of the daemon or CLI                   | Adapters refuse to read API keys from environment variables; the sole key source is the OS keyring loaded on-demand. Verified by unit tests that mock the keyring to `None`. | v0.1   |
| Query and embedding content sent to cloud provider    | Information disclosure | Cloud adapter forwards facet content to T7 for embedding or reranking | At v0.4 (per ADR-0014) every cloud adapter is removed from the codebase. The shipped adapter set is fastembed (in-process ONNX Runtime) for both embedder and reranker. There is no enabled-by-default cloud surface and no cloud adapter module to import; the threat is mitigated by absence rather than by consent gating. | v0.4   |
| Adapter silently returns mismatched embedding dim     | Integrity              | Provider changes ``dim`` on a model upgrade; vault writes short/long vectors into ``vec_<id>`` | Embedder adapters verify observed ``dim`` against the registered value and raise ``AdapterResponseError`` on mismatch. The ``vec_<id>`` virtual table's ``FLOAT[<dim>]`` constraint rejects the write even if the adapter check is bypassed.         | v0.1   |
| Unclassified adapter failure masks retry decision     | Availability / integrity | Adapter raises a generic ``Exception``; embed worker can't tell network error from OOM from auth | Adapter errors are classified into a narrow taxonomy (``AdapterNetworkError``, ``AdapterModelNotFoundError``, ``AdapterOOMError``, ``AdapterAuthError``, ``AdapterResponseError``). The P3 embed worker dispatches retry policy on the class, not a string match.        | v0.1   |
| Outbound traffic beyond configured adapters           | Information disclosure | Dependency makes unexpected network call (telemetry, update check)    | CI network-policy test: run full test suite with all outbound blocked. Since v0.4 there is no expected outbound traffic at all — fastembed runs in-process and downloads weights only on first model registration via the user-controlled `tessera models set --activate` step. The CI gate asserts the test suite passes with all outbound denied. | v0.1   |

### S6 — Sync backend (T8), v0.5+

| Threat                       | Category                      | Vector                                                          | Mitigation                                                                                                                  | Status |
| ---------------------------- | ----------------------------- | --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------ |
| Sync backend reads plaintext | Information disclosure        | BYO S3 provider or attacker with credentials reads synced blobs | Client-side envelope encryption with key held locally; key never transmitted.                                               | v0.5   |
| Sync backend modifies blobs  | Tampering                     | Provider alters synced payload                                  | Per-blob MAC verified on pull; mismatch aborts restore and surfaces error.                                                  | v0.5   |
| Replay of old sync state     | Tampering                     | Attacker restores an earlier vault snapshot                     | Monotonic sync-sequence number per vault, signed with local key; regression detected on restore.                            | v0.5   |
| Last-writer-wins silent drop | Information disclosure (loss) | Simultaneous captures on two devices collapse to one facet      | Append-on-conflict for facets (dedup by content_hash only); manual merge for entities only. See release-spec.md §v0.5 sync. | v0.5   |

### S7 — Right-to-erasure and soft delete

| Threat                                 | Category                            | Vector                                                                                        | Mitigation                                                                                                                                                                                              | Status |
| -------------------------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| "Forget" does not actually forget      | Compliance / information disclosure | User requests erasure; soft-delete leaves content in `facets.content`, FTS5 index, vec tables | Hard-delete path: `tessera vault purge <external_id>` removes row, FTS row, vec row, and hashes the corresponding audit entries. Soft delete is the default; hard delete is the opt-in compliance path. | v0.1   |
| Embedding inversion of deleted content | Information disclosure              | Vector survives after row delete                                                              | Hard-delete cascades to the active `vec_*` virtual table; inactive vec tables retain until `tessera vault prune-old-models`.                                                                            | v0.1   |

## Out of scope

- **Nation-state adversaries with physical access and coercion.** Tessera does not claim duress protection.
- **Kernel or hypervisor compromise.** If the OS is compromised, the vault is compromised. No mitigations at user-space can change this.
- **Side-channel attacks on the encryption key while unlocked.** Mitigations exist (mlock, no swap) but a rootkit on an unlocked machine wins.
- **Social engineering of the user's passphrase.** The passphrase is user-managed.
- **Deepfake of user voice instructing the agent.** Tessera stores identity; it does not verify the identity of the humans instructing the agent.

## Mitigation verification

Each mitigation above has a corresponding test or explicit verification artifact. The mapping is tracked in `tests/security/README.md` (to be created in v0.1 dev cycle). A mitigation without a test does not count as delivered.

| Verification type           | Examples                                                                                 |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| Unit / integration test     | Token hashing, scope enforcement, FK constraints, CSRF header check, hard-delete cascade |
| Network policy test in CI   | Outbound block test, localhost-only binding                                              |
| Manual audit on release     | Keyring integration, Origin-header rejection of browsers, mlock of unlock key            |
| External review (post-v0.3) | Encryption primitive choice, HMAC chain construction                                     |

## Revision policy

- Threats added when a new feature is proposed; the PR that adds the feature updates this document.
- Threats removed only by explicit deprecation of the surface (e.g., if HTTP MCP is replaced by Unix-socket-only, S3 threats are reworked).
- Mitigation deadlines slip with a dated note; slipping past a major version requires an explicit reassessment.
