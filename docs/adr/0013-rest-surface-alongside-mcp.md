# ADR 0013 — REST surface alongside MCP

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

ADR-0005 picked MCP as the primary transport because every major agent runtime — Claude Desktop, Claude Code, Cursor, Codex, ChatGPT Developer Mode (eventually) — speaks MCP natively. That decision still holds for tool clients. It does not hold for two emerging integration patterns:

1. **Pre-prompt and post-prompt hooks** in agent runtimes (Claude Code's hook system, Cursor's pre-completion hooks). These run as shell scripts, fire on every prompt, and inject text into the model's context before the model sees it. Every MCP tool call out of a hook costs an extra ~50–150 tokens of JSON-RPC envelope plus the schema preamble that lives in the client's context for the entire session.

2. **User-authored skills and shell scripts** that want to talk to the daemon without depending on an MCP client. The current path forces users either to embed the MCP request shape (`{"method": "...", "args": {...}}` over `POST /mcp`) or to write a Python wrapper that imports `tessera.cli.tools_cmd`.

Three candidate responses:

| Option | What changes | Cost | Reversibility |
|---|---|---|---|
| Stay MCP-only; users write wrappers | Nothing in the daemon | Each hook author writes the same envelope-stripping wrapper | Free |
| Drop MCP entirely, switch to REST | Re-pitch the project; rip out connectors, stdio bridge, ~30% of docs | High; breaks every existing integration | Hard fork to undo |
| REST surface alongside MCP, sharing one dispatcher | One more router in the daemon; ~150 lines of glue | Low; both surfaces share auth + scope + storage | Trivial — `git rm rest.py` retires it |

## Decision

**Add a REST surface at `/api/v1/*` alongside the existing `/mcp` endpoint.** Both surfaces share the same daemon dispatcher, the same capability-token auth, and the same scope checks. The MCP surface stays unchanged for clients that auto-discover tools. The REST surface exists for hooks, skills, scripts, and any consumer that wants curl-shaped access without the JSON-RPC envelope.

Endpoints:

```
POST   /api/v1/capture                  body: {content, facet_type, ...}
GET    /api/v1/recall?q=&k=&facet_types=
GET    /api/v1/stats
GET    /api/v1/facets?facet_type=&limit=&since=
GET    /api/v1/facets/<external_id>     (show)
DELETE /api/v1/facets/<external_id>?reason=  (forget)
POST   /api/v1/skills                   body: {name, description, procedure_md}
GET    /api/v1/skills?active_only=&limit=
GET    /api/v1/skills/<name>            (get_skill)
GET    /api/v1/people?limit=&since=
GET    /api/v1/people/resolve?mention=
```

Response shape on success: dispatcher result dict directly, status 200. On failure: `{"error": {"code", "message"}}` with the appropriate 4xx/5xx status. No top-level `ok` flag — the URL plus status code carry the success signal that `ok` carries on the MCP wire.

## Rationale

1. **One dispatcher, two transports.** The existing dispatcher in `daemon/dispatch.py` was already documented as transport-agnostic — its docstring calls out that the stdio bridge already reuses it. Adding a REST router does not duplicate any business logic; it adds one more way to reach the same handlers.

2. **The token tax is real for hooks, immaterial for tool clients.** Tool schemas live in client context once per session. A user calling recall five times in a Claude Desktop chat pays the schema cost amortised across the whole session. A pre-prompt hook firing on every prompt pays the envelope cost on every call — and a daily user firing 100+ hook calls is realistic, so 5–15k tokens per day is realistic.

3. **Curl-friendly is hook-friendly.** Hook authors write shell scripts. `curl -s "http://127.0.0.1:5710/api/v1/recall?q=$query" -H "Authorization: Bearer $TESSERA_TOKEN" | jq -r '.matches[].snippet'` is a one-liner; the equivalent over MCP needs `jq` to construct the JSON-RPC body and `jq` again to unwrap the result envelope.

4. **Optionality preservation.** Dropping MCP outright would orphan every existing integration and force a re-pitch. Keeping MCP as a peer surface preserves the category claim ("portable context for every AI tool") while opening the new audience. If MCP usage drops to zero in real user signal over the next two releases, retiring the shim is one commit; restoring it after a drop would be a re-fork.

## Consequences

**Positive:**
- Hook and skill authors get a curl-friendly recipe surface. The token tax on high-frequency calls drops to zero envelope overhead.
- Existing MCP clients (Claude Desktop, Cursor, Codex, future ChatGPT) keep working. No migration. No connector changes. No README pivot.
- The dispatcher gains a forcing function for transport-agnosticity — every new feature now has to be expressible in both surfaces, which keeps the dispatcher contract clean.

**Negative:**
- Two API surfaces to keep in sync at the wire layer. Mitigated by sharing the dispatcher: business logic is identical; only the request-shape parsing and response-shape rendering differ.
- The lean error envelope on REST (`{"error": {...}}`) is intentionally different from MCP's (`{"ok": false, "error": {...}}`). Users who write tooling against both must handle both; documented in `docs/api.md`.
- ADR-0005's "MCP as primary" framing is now narrower. ADR-0005 stays accepted for tool-client integrations; this ADR scopes the REST surface to hook/script integrations.

## Boundary with ADR-0005

ADR-0005 specifies MCP for **tool-client integrations** — the agent runtime auto-discovers Tessera tools and surfaces them in its UI. This ADR specifies REST for **direct HTTP integrations** — a script, hook, or skill calls the daemon over curl. Both ADRs accepted; both surfaces shipped; neither replaces the other.

## Alternatives considered

- **Drop MCP entirely** — rejected. Breaks every existing integration, requires re-pitching the project, and the token-tax problem the user wants solved is solved entirely by adding REST without subtracting MCP.
- **REST as MCP transport (HTTP REST verbs that wrap MCP envelopes)** — rejected. Gains no token savings (envelope stays), adds a translation layer with no new value.
- **OpenAPI-generated client SDKs** — rejected for v0.3. Hook authors write shell scripts; an SDK is the wrong shape. Reconsiderable for v0.5 if a real SDK consumer emerges.
