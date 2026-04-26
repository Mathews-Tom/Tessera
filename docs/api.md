# Tessera REST API

Curl-friendly HTTP surface for hooks, skills, scripts, and any consumer that wants direct access to the Tessera daemon without the MCP JSON-RPC envelope. All endpoints require a bearer token; all responses are JSON.

The MCP surface at `/mcp` continues to serve auto-discovering AI tool clients (Claude Desktop, Cursor, Codex). See [`docs/adr/0013-rest-surface-alongside-mcp.md`](adr/0013-rest-surface-alongside-mcp.md) for why both surfaces ship side by side.

## Setup

The daemon binds `127.0.0.1:5710` by default; override with `--port` on `tessera daemon start` or `$TESSERA_HTTP_PORT`.

Mint a long-lived service token once and export it for hook scripts:

```bash
tessera tokens create \
  --client-name cli \
  --token-class service \
  --read '*' --write '*' \
  --token-ttl-days 30
# copy the printed token (shown once) into your shell rc:
export TESSERA_TOKEN="paste-the-token-here"
export TESSERA_DAEMON_URL="http://127.0.0.1:5710"   # optional override
```

Every example below assumes `$TESSERA_TOKEN` is set. The `tessera curl` subcommand prints copy-pasteable recipes for each endpoint:

```bash
tessera curl --print recall "LinkedIn post" --k 5
# emits the literal curl invocation; safe to paste into hook scripts
# because the bearer header stays as ${TESSERA_TOKEN} (unexpanded).
```

Drop `--print` to execute the recipe and get back the response JSON.

## Common contract

- **Auth:** every endpoint requires `Authorization: Bearer <token>`. Tokens come from `tessera tokens create`.
- **Origin gate:** if the client sends an `Origin` header, it must match the daemon's allowlist (`http://localhost`, `http://127.0.0.1`, `null` by default). Native clients (curl, scripts) typically omit `Origin` and pass through.
- **Success shape:** the dispatcher's result dict directly as the response body. HTTP 200.
- **Error shape:** `{"error": {"code": "...", "message": "..."}}` with one of these statuses:
  - `400 invalid_input` — missing or malformed argument
  - `401 invalid_input` — missing bearer token
  - `401 scope_denied` — invalid or revoked token
  - `403 scope_denied` — token valid but lacks scope for the operation
  - `404 unknown_method` — unknown route
  - `405 invalid_input` — wrong HTTP method for the path
  - `500 storage_error` — vault write/read failure
  - `500 internal_error` — uncaught exception (the type name is exposed; the message is suppressed)

No top-level `ok` flag — the HTTP status code carries the success signal.

## Endpoints

### `POST /api/v1/capture`

Write a new facet. Idempotent on `(content, facet_type)`: a duplicate returns the existing row's external id with `is_duplicate: true`.

```bash
curl -s -X POST 'http://127.0.0.1:5710/api/v1/capture' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{"content": "I prefer uv over pip for Python.", "facet_type": "preference"}'
```

Body: `{content: string, facet_type: string, source_tool?: string, metadata?: object}`. Response: `{external_id, is_duplicate, facet_type}`.

Required scope: `write` on `facet_type`.

### `GET /api/v1/recall`

Cross-facet hybrid recall with SWCR ordering. Returns a budgeted bundle of matches.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/recall?q=LinkedIn+post&k=10' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" | jq '.matches'
```

Query params:

- `q` (required) — natural-language query text (alias: `query_text`).
- `k` (optional, default 10) — number of matches to return.
- `facet_types` (optional) — comma-separated list, or repeated parameter. Defaults to every type the token can read.
- `requested_budget_tokens` (optional) — override the response token budget.

Response: `{matches: [...], warnings: [...], seed: int, truncated: bool, rerank_degraded: bool, total_tokens: int}`.

Required scope: `read` on each requested `facet_type`.

### `GET /api/v1/stats`

Vault-wide counters and embed-worker health. No params.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/stats' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Response: `{embed_health: {pending, embedded, failed, stale}, by_source: {...}, active_models: [...], vault_size_bytes, facet_count}`.

Required scope: any `read`.

### `GET /api/v1/facets`

List facets of a given type, newest first.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/facets?facet_type=style&limit=20' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `facet_type` (required), `limit` (default 20), `since` (optional epoch seconds).

Response: `{items: [...], truncated, total_tokens}`.

Required scope: `read` on `facet_type`.

### `GET /api/v1/facets/<external_id>`

Fetch a single facet by external id.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/facets/01HXY...' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Response: `{external_id, facet_type, snippet, captured_at, source_tool, embed_status, token_count}`.

Required scope: `read` on the row's `facet_type`.

### `DELETE /api/v1/facets/<external_id>`

Soft-delete a facet (audit-logged, reversible at SQL layer).

```bash
curl -s -X DELETE 'http://127.0.0.1:5710/api/v1/facets/01HXY...?reason=rotated' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `reason` (optional).

Response: `{external_id, facet_type, deleted_at}`.

Required scope: `write` on the row's `facet_type`.

### `POST /api/v1/skills`

Author a new skill (named procedure markdown).

```bash
curl -s -X POST 'http://127.0.0.1:5710/api/v1/skills' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{"name": "git-rebase-cleanup", "description": "...", "procedure_md": "..."}'
```

Body: `{name, description, procedure_md, source_tool?}`. Response: `{external_id, name, is_new}`.

Required scope: `write` on `skill`.

### `GET /api/v1/skills`

List skills.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/skills?active_only=true&limit=50' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `active_only` (default `true`), `limit` (default 50).

Response: `{items: [...], truncated, total_tokens}`.

Required scope: `read` on `skill`.

### `GET /api/v1/skills/<name>`

Fetch one skill by exact name.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/skills/git-rebase-cleanup' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Response: `{skill: {...}}` or `{skill: null}` when no live row matches.

Required scope: `read` on `skill`.

### `GET /api/v1/people`

List people referenced in your facets.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/people?limit=50' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `limit` (default 50), `since` (optional epoch seconds).

Response: `{items: [...], truncated, total_tokens}`.

Required scope: `read` on `person`.

### `GET /api/v1/people/resolve`

Resolve a free-form mention to candidate people.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/people/resolve?mention=Daisy' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `mention` (required).

Response: `{matches: [...], is_exact: bool}`. `is_exact: true` when there is a single canonical-name or alias match; otherwise `matches` lists every candidate and the caller should ask the user.

Required scope: `read` on `person`.

## Recipes

### Pre-prompt hook (Claude Code)

Inject the top-3 recall results into every prompt:

```bash
#!/usr/bin/env bash
# ~/.claude/hooks/pre-prompt-tessera.sh
query="$(echo "$CLAUDE_PROMPT" | head -c 200)"
context="$(curl -s "http://127.0.0.1:5710/api/v1/recall?q=$(printf %s "$query" | jq -sRr @uri)&k=3" \
                -H "Authorization: Bearer ${TESSERA_TOKEN}" \
           | jq -r '.matches[] | .snippet')"
if [[ -n "$context" ]]; then
  echo "Tessera context for this prompt:"
  echo "$context"
fi
```

Cost per prompt: one HTTP round-trip plus the snippet text. No MCP envelope, no schema preamble.

### Post-tool capture hook

Save corrected outputs back to Tessera:

```bash
#!/usr/bin/env bash
# ~/.claude/hooks/post-tool-tessera.sh — fires after the user accepts an edit
content="$(jq -Rs .)"   # whatever the hook receives on stdin
curl -s -X POST 'http://127.0.0.1:5710/api/v1/capture' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H "Content-Type: application/json" \
     -d "{\"content\": $content, \"facet_type\": \"preference\"}"
```

### Daily backup script

```bash
#!/usr/bin/env bash
# ~/bin/tessera-daily.sh — run from cron / launchd
date_stamp="$(date +%F)"
cp ~/.tessera/vault.db ~/Backups/tessera-"$date_stamp".db
curl -s 'http://127.0.0.1:5710/api/v1/stats' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     | jq '.embed_health, .facet_count'
```

## Boundary with `/mcp`

The `/mcp` endpoint stays for MCP-aware clients (Claude Desktop, Cursor, Codex). The two surfaces share auth, scope, and the dispatcher; they differ only in the request shape (path + verb + query/body for REST, JSON-RPC body for MCP) and the response envelope (lean dict for REST, `{"ok": true, "result": ...}` for MCP). Use whichever fits the consumer:

- AI tool client that auto-discovers tools → `/mcp` (no work; `tessera connect` writes the config).
- Hook, skill, shell script, cron job, third-party automation → `/api/v1/*`.

Both surfaces accept tokens minted by the same `tessera tokens create`; the same scopes apply.
