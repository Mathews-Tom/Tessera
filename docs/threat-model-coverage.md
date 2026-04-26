# Threat-Model Coverage Audit (P14)

**Purpose:** satisfy P14 task 2 in `.docs/development-plan.md` — every `v0.1`-tagged mitigation in `docs/threat-model.md` maps to a specific test path or script that exercises it, or to code that enforces it structurally. Gaps are recorded explicitly and tracked as follow-ups.

**Audit date:** 2026-04-24
**Scope:** mitigations marked `v0.1` in `docs/threat-model.md`. Later-milestone mitigations (`v0.3`, `v0.5`, `v1.0`) are out of scope for this audit.

---

## S1 — Vault file

| Threat | Mitigation | Evidence | Status |
|--------|------------|----------|--------|
| Offline read of stolen vault | Encryption at rest via sqlcipher + argon2id-derived key | `tests/security/test_vault_encryption_rest.py` — on-disk bytes do not reveal plaintext; `src/tessera/vault/encryption.py` — argon2id KDF | Covered |
| Offline modification of vault | Encryption at rest (HMAC chain is v0.3 work) | Same as above | Covered for v0.1 scope |
| Accidental exposure via user action | Default encryption, docs warn | `src/tessera/migration/__init__.py::bootstrap` — the bootstrap path has no code path that writes plaintext; README + `docs/system-overview.md` carry the user-facing warning | Covered |
| Cross-tool scope leak | Per-facet-type scope check on every MCP tool call | `src/tessera/auth/scopes.py` + `tests/unit/test_auth_scopes.py` (build_scope validation, empty-scope-denies-everything); `tests/integration/test_mcp_tool_surface.py` exercises the dispatch path | Covered |

## S2 — Capability tokens

| Threat | Mitigation | Evidence | Status |
|--------|------------|----------|--------|
| Token theft from client config | Short TTL (15–60 min access, 7 day refresh) | `tests/unit/test_auth_tokens.py::test_ttl_constants_match_adr_0007` asserts session=30min, service=24h, subagent=15min | Covered |
| Token in URL via ChatGPT Dev Mode | Docs warn; P7 exchange endpoint hands off to bearer | `tests/security/test_exchange_endpoint.py` — nonce exchange, 30s TTL, single-use; `docs/system-design.md §URL-embedded tokens` carries the warning | Covered |
| Revocation lag on in-flight sessions | Every MCP request re-validates token against `revoked_at` | `src/tessera/auth/tokens.py::verify_and_touch` calls the `capabilities` table on every request; `tests/unit/test_auth_tokens.py` covers the verify → revoke → re-verify sequence | Covered |
| Hash weakness | Per-token random salt: `sha256(salt \|\| token)` | `src/tessera/auth/tokens.py::_hash_token` — salt + token concatenated before sha256; stored salt distinct per row | Covered |

## S3 — Daemon process and MCP transport

| Threat | Mitigation | Evidence | Status |
|--------|------------|----------|--------|
| Co-located process queries HTTP MCP on loopback | Unix socket is the default control plane; HTTP MCP is opt-in | `src/tessera/daemon/control.py` binds socket at `$XDG_RUNTIME_DIR/tessera/tessera.sock` with mode 0600; `src/tessera/daemon/config.py::DaemonConfig.http_port` starts empty (no HTTP bind unless configured) | Covered |
| Co-located user on multi-user host | Socket mode 0600 | `src/tessera/daemon/control.py::serve_control_socket` sets `os.chmod(socket_path, 0o600)` immediately after bind; `tests/integration/test_daemon_supervisor.py` exercises the full bind path | Covered (code enforces mode; no dedicated mode-assertion test — tracked below) |
| Arbitrary tool invocation via CSRF from browser | Origin-header gate on HTTP MCP | `src/tessera/daemon/http_mcp.py` — `_check_origin` rejects requests whose `Origin` header is not in the configured allowlist; `tests/integration/test_daemon_http_mcp.py` covers allowed / denied origins | Covered |
| Browser identified via user-agent | MCP clients announce themselves; browsers are rejected | Same origin-gate pathway enforces this structurally | Covered |

## S4 — Audit log

| Threat | Mitigation | Evidence | Status |
|--------|------------|----------|--------|
| Audit log PII leak | Closed allowlist of payload keys per op | `src/tessera/vault/audit.py::_PAYLOAD_ALLOWLIST` + `write` raises `DisallowedPayloadKeyError` on any extra key; `tests/unit/test_audit_log.py` covers the allowlist enforcement on every emitter | Covered |
| Diagnostic bundle content leak | Scrubber strips content before bundle export | `src/tessera/observability/bundles.py` + `tests/security/test_bundle_scrubber.py` | Covered |

## S5 — Model adapters and cloud providers

| Threat | Mitigation | Evidence | Status |
|--------|------------|----------|--------|
| API key in config file readable by co-located process | OS keyring (Keychain / secret-service / Credential Manager) stores keys; config stores a reference | `src/tessera/vault/keyring_cache.py` + `tests/unit/test_keyring_cache.py` | Covered |
| API key in process env readable by co-located process | No cloud adapter ships in v0.4 onward — there is no API-key surface to expose. fastembed runs in-process. | ADR-0014 records the cloud-adapter removal; no test artefact is needed because the threat surface is gone. | Covered (by removal) |
| Query and embedding content sent to cloud provider | No cloud adapter ships in v0.4 onward; fastembed embeds and reranks in-process | `src/tessera/adapters/__init__.py` does not import any cloud adapter (none exist after ADR-0014); `.github/workflows/ci.yml::no-outbound` job blocks non-loopback traffic and runs the full suite to prove transitive deps stay local | Covered (by removal) |
| Adapter silently returns mismatched embedding dim | Adapter verifies observed dim against registered value | `src/tessera/adapters/fastembed_embedder.py::FastEmbedEmbedder.embed` raises `AdapterResponseError` on dim mismatch; `tests/unit/test_fastembed_embedder.py::test_embed_dim_mismatch_raises_response_error` | Covered |
| Unclassified adapter failure masks retry decision | Narrow error taxonomy | `src/tessera/adapters/errors.py` + `tests/unit/test_retry_policy.py` covers dispatch-by-class for every error | Covered |
| Outbound traffic beyond configured adapters | CI network-policy test | `.github/workflows/ci.yml::no-outbound` job (visible in the CI check list on PR #17 as "No-outbound network test") runs the full suite with a socket-layer guard blocking every destination except loopback | Covered |

## S7 — Right-to-erasure and soft delete

| Threat | Mitigation | Evidence | Status |
|--------|------------|----------|--------|
| "Forget" does not actually forget | Hard-delete path + FTS + vec cascade | `tests/security/test_hard_delete_cascade.py` covers the full cascade; `src/tessera/vault/facets.py::hard_delete` implements it; `docs/release-spec.md` soft-delete is default, hard-delete is opt-in | Covered |
| Embedding inversion of deleted content | Hard-delete cascades to active `vec_*` virtual table | Same test | Covered |

---

## OWASP MCP-over-HTTP self-audit

Cross-check of the v0.1 HTTP MCP surface against the OWASP Top 10 attack classes relevant to loopback RPC endpoints.

| OWASP class | Relevance to MCP-over-HTTP | Mitigation | Evidence |
|-------------|----------------------------|------------|----------|
| A01 Broken access control | Capability tokens scope every call | Per-tool, per-scope, per-facet-type check | `src/tessera/auth/scopes.py`, `tests/unit/test_auth_scopes.py` |
| A02 Cryptographic failures | Key material on disk in plaintext | sqlcipher at rest, argon2id KDF, keyring for cloud API keys | `tests/security/test_vault_encryption_rest.py`, `tests/unit/test_keyring_cache.py` |
| A03 Injection | SQL injection via tool args | All vault DB calls go through parameterised statements; no f-string SQL | Code review: `src/tessera/vault/*.py` uses `?` placeholders exclusively; `src/tessera/vault/export.py::_fetch_facets` is the only dynamic SQL and interpolates only a constant `WHERE` fragment, not user input |
| A04 Insecure design | Trusting the loopback | Origin header + socket mode 0600 + opt-in HTTP MCP | `src/tessera/daemon/http_mcp.py`, `tests/integration/test_daemon_http_mcp.py` |
| A05 Security misconfiguration | Default config enables features the user did not ask for | HTTP MCP off by default; cloud adapters not auto-imported; telemetry never shipped | CI `No-telemetry grep` job; `src/tessera/daemon/config.py` default ports |
| A06 Vulnerable components | Dependencies with known CVEs | `uv.lock` pins every transitive dep; Dependabot/renovate not yet configured (pre-v0.1.x) | Gap — tracked below |
| A07 Identification & authentication failures | Token replay / session fixation | Per-token salt + re-validation on every call; URL exchange endpoint with 30-second nonce TTL | `src/tessera/auth/tokens.py`, `tests/security/test_exchange_endpoint.py` |
| A08 Software & data integrity | Tampered vault | Encryption at rest (HMAC chain is v0.3) | Same as S1 |
| A09 Logging failures | Content leak via logs | Audit allowlist; bundle scrubber | `src/tessera/vault/audit.py`, `tests/security/test_bundle_scrubber.py` |
| A10 SSRF | Daemon makes server-side requests on behalf of a caller | No SSRF surface: after ADR-0014 the daemon makes no outbound calls. fastembed runs in-process; weight downloads are user-initiated via `tessera models set --activate` and reach only the model registry (HuggingFace) at the user's request, not on caller's behalf. | Code review; `no-outbound` CI job proves no caller-driven outbound paths |

---

## Gaps and follow-ups

Three items surfaced during the audit. None are P14-blocking; all are tracked as follow-ups for v0.1.x.

1. **Socket mode assertion.** S3's 0600-mode mitigation is enforced structurally in `serve_control_socket`, but no test asserts `Path(socket_path).stat().st_mode & 0o777 == 0o600` after the bind. A one-line assertion in `tests/integration/test_daemon_supervisor.py::test_supervisor_starts_serves_and_stops` would close this. (Follow-up: v0.1.x.)
2. **Dependency CVE scanning.** OWASP A06 mitigation relies on the solo dev rebasing `uv.lock` on fresh resolutions. Automating this with `pip-audit` in CI on a weekly cron is the v0.1.x fix. (Follow-up: v0.1.x.)
3. **HMAC-chained audit log.** S1's "tampering detectable" clause is v0.3 scope per the threat model; deferred explicitly in the Status column.

---

## How this audit was built

Every row in the tables above was verified by reading the referenced file or test source. No row cites code that does not currently exist in the repo. When a mitigation was structurally enforced (e.g. socket mode, default import paths), the evidence column points to the code that enforces it rather than a test — not every structural enforcement needs a test if the structure is simple enough that a reviewer can verify it by inspection.

Next P14 re-audit happens before the v0.1 release tag is cut. Any mitigation that has moved or been renamed since this audit must be re-verified at that checkpoint.
