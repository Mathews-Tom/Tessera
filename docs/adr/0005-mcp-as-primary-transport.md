# ADR 0005 — MCP as primary transport

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

Tessera's vault must be accessible to any agent — autonomous or human-driven — that wants to read or write identity. Candidate transports:

| Transport | Standard | Agent support | Auth model | Complexity |
|---|---|---|---|---|
| MCP (Model Context Protocol) | Anthropic spec, widely adopted | Claude Desktop, Claude Code, Cursor, Codex, ChatGPT Dev Mode, Cline, OpenClaw, Letta | Per-connection capability tokens | Moderate |
| REST + OpenAPI | Universal | Universal, but each agent hand-writes a client | Bearer tokens | Low |
| gRPC | Broad | Rare in agent runtimes | mTLS or bearer | High |
| Direct SQLite access | Native | Any library with SQLite bindings | Filesystem perms | Low, but wrong semantics |
| LangChain Tool / OpenAI Function-Calling schemas | Per-framework | Per-framework | Framework-level | Fragmented |

## Decision

**MCP over HTTP on 127.0.0.1:5710 as the primary transport.** Unix socket (`~/.tessera/tessera.sock`) for the CLI control plane. MCP stdio bridge as a compatibility path for clients that don't speak HTTP MCP cleanly (ships in v0.1.x).

## Rationale

1. **Agent-runtime alignment.** By April 2026, every major agent runtime speaks MCP natively. Writing a custom protocol means every user writes a custom client. MCP lets Claude Code, Cursor, Codex, and ChatGPT Dev Mode connect with a config file change.
2. **Tool-surface is the right abstraction.** Identity operations (`capture`, `recall`, `assume_identity`) map cleanly to MCP tools. REST would require designing URL structures, parameter encoding, and error conventions that MCP already specifies.
3. **Auth fits the scoped-capability model.** MCP connections already carry a session identity; Tessera's capability token attaches to the session. Per-scope, per-facet-type authorization is expressible in the existing MCP tool-call flow.
4. **Localhost-first matches local-first.** HTTP on loopback is zero-config on every platform. No certificates, no DNS, no tunneling. Unix sockets for CLI control plane keep privileged operations off the network interface entirely.

## Consequences

**Positive:**
- Most users integrate with a single config file edit. No custom SDK, no client-code generation.
- Any future MCP-speaking agent is a first-class client for free.
- MCP's tool-definition schema doubles as public API contract; tools can be introspected programmatically.

**Negative:**
- MCP spec is evolving. Breaking changes in the spec force Tessera to track them. Mitigated by pinning to a specific spec version per Tessera release and documenting the pin.
- HTTP on loopback is reachable by any local process on a shared machine. Threat model requires Unix-socket-only mode for multi-user hosts. Documented in `docs/threat-model.md`.
- ChatGPT Developer Mode's URL-embedded token is an MCP-ecosystem antipattern Tessera must warn against. See ADR on token lifecycle (forthcoming).

## Transport binding details

| Surface | Bind | Purpose | Auth |
|---|---|---|---|
| HTTP MCP | `127.0.0.1:5710` (configurable) | Agent tool calls | Capability token in header |
| Unix socket | `$XDG_RUNTIME_DIR/tessera/tessera.sock` (mode 0600) | CLI control | Filesystem permissions |
| Stdio MCP bridge (v0.1.x) | child-process pipes | Clients without HTTP MCP support | Pipe inheritance |

**Default posture for v0.1**: HTTP MCP bound to loopback, warn on multi-user login sessions. v0.1.x introduces Unix-socket MCP mode for clients that support it.

## Alternatives considered

- **REST + OpenAPI**: Universal but each client is hand-rolled. For a solo-dev project, the client-writing burden falls on every user. Rejected.
- **gRPC**: Overkill for localhost, poor agent-runtime coverage. Rejected.
- **Direct SQLite access**: Would mean every client implements retrieval, auth, audit. Contradicts the "daemon owns the vault" architecture. Rejected.
- **Framework-specific schemas (LangChain, etc.)**: Fragmented; writing N adapters. MCP subsumes them. Rejected.

## Revisit triggers

- MCP spec forks or is superseded by a different standard adopted by >50% of agent runtimes.
- A user class emerges (e.g., mobile agents) for which MCP-over-HTTP is not available.
- Loopback-HTTP threat model proves insufficient and Unix-socket-only becomes the default.
