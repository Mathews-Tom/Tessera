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

Cross-facet hybrid recall with SWCR ordering. Returns a budgeted bundle of matches. When Tessera has no trustworthy context to return, it returns an empty `matches` array and a stable `degraded_reason` instead of padding the bundle.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/recall?q=LinkedIn+post&k=10' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" | jq '.matches'
```

Query params:

- `q` (required) — natural-language query text (alias: `query_text`).
- `k` (optional, default 10) — number of matches to return.
- `facet_types` (optional) — comma-separated list, or repeated parameter. Defaults to every type the token can read.
- `requested_budget_tokens` (optional) — override the response token budget.

Response: `{matches: [...], warnings: [...], degraded_reason: string|null, seed: int, truncated: bool, rerank_degraded: bool, total_tokens: int}`.

`degraded_reason` is `null` when at least one match is returned or when an empty result is explained by another explicit response flag such as `truncated`. Stable enum values:

- `empty_vault` — the requested readable facet set contains no live facets.
- `no_signal_above_floor` — live facets exist, but every candidate scored at or below the recall relevance floor, so no context is returned.

Stable `warnings` entries (a non-exhaustive list — callers should treat the array as an open enum):

- `reranker_degraded: falling back to RRF order` — cross-encoder reranker failed health check; bundle reverted to RRF order.
- `token_budget_truncated` — snippet budget cut the bundle short of `k`.
- `compiled_artifact_stale: <n> match(es) are stale` — at least one `compiled_notebook` row in the response carries `is_stale=true`. Per the Playbook retrieval and staleness contract, stale Playbooks remain inspectable but never authoritative; the caller decides whether to recompile or to route the answer through raw recall instead. There is no silent fallback. See `docs/system-design.md §Playbook retrieval and staleness contract`.

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

### `POST /api/v1/agent_profiles`

Register an `agent_profile` facet (V0.5-P2 / ADR 0017). Validates the structured metadata shape and, by default, repoints `agents.profile_facet_external_id` at the new facet so subsequent `recall` calls surface it as the agent's canonical profile.

```bash
curl -s -X POST 'http://127.0.0.1:5710/api/v1/agent_profiles' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H 'Content-Type: application/json' \
     -d '{
       "content": "The digest agent compiles weekly engineering updates.",
       "metadata": {
         "purpose": "summarize standups into a weekly digest",
         "inputs": ["daily standup notes"],
         "outputs": ["weekly digest markdown"],
         "cadence": "weekly",
         "skill_refs": []
       }
     }'
```

Body: `content` (required, ≤ 65 536 chars), `metadata` (required object: `purpose`, `inputs[]`, `outputs[]`, `cadence`, `skill_refs[]`, optional `verification_ref`), `source_tool` (optional, defaults to the capability's client name), `set_active_link` (optional bool, default `true`).

Response: `{external_id, is_new, is_active_link}`.

Required scope: `write` on `agent_profile`.

### `GET /api/v1/agent_profiles`

List the calling agent's profile facets, ordered by capture time descending.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/agent_profiles?limit=20' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `limit` (optional, default 20, max 100), `since` (optional epoch).

Response: `{items: [{external_id, purpose, cadence, skill_refs, captured_at, is_active_link}], truncated, total_tokens}`.

Required scope: `read` on `agent_profile`.

### `GET /api/v1/agent_profiles/<external_id>`

Fetch one agent_profile by external_id. Cross-agent reads return `{profile: null}` even when the ULID is leaked.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/agent_profiles/01HXY...' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Response: `{profile: {external_id, content, purpose, inputs, outputs, cadence, skill_refs, verification_ref, captured_at, embed_status, is_active_link, truncated, token_count}}` or `{profile: null}` when no live profile matches.

Required scope: `read` on `agent_profile`.

### `POST /api/v1/checklists`

Register a `verification_checklist` facet (V0.5-P3 / ADR 0018) — the pre-delivery gate an agent runs before declaring a task done. Tessera stores the checklist; the agent or its caller-side runner executes it.

```bash
curl -s -X POST 'http://127.0.0.1:5710/api/v1/checklists' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H 'Content-Type: application/json' \
     -d '{
       "content": "Pre-delivery gate for the digest agent.",
       "metadata": {
         "agent_ref": "01HZX1Y2Z3MNPQRSTVWXYZ0123",
         "trigger": "pre_delivery",
         "checks": [
           {"id": "tests", "statement": "Tests cover new branches", "severity": "blocker"},
           {"id": "changelog", "statement": "Changelog entry present", "severity": "warning"}
         ],
         "pass_criteria": "All blockers green; warnings annotated"
       }
     }'
```

Body: `content` (required), `metadata` (required object: `agent_ref` ULID, `trigger`, `checks[]` of `{id, statement, severity}` with severity ∈ `{blocker, warning, informational}`, `pass_criteria`), `source_tool` (optional).

Response: `{external_id, is_new}`.

Required scope: `write` on `verification_checklist`. Cross-agent `agent_ref` references are rejected with `invalid_input`.

### `POST /api/v1/retrospectives`

Record a `retrospective` facet — the post-run reflection on what worked, what gapped, and what changes the agent or user wants next time.

```bash
curl -s -X POST 'http://127.0.0.1:5710/api/v1/retrospectives' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H 'Content-Type: application/json' \
     -d '{
       "content": "The summary missed the migration risk in PR #102.",
       "metadata": {
         "agent_ref": "01HZX1Y2Z3MNPQRSTVWXYZ0123",
         "task_id": "digest_2026_05_03",
         "went_well": ["captured the digest", "no flake"],
         "gaps": ["missed migration risk"],
         "changes": [
           {"target": "verification_checklist", "change": "Add ALTER TABLE scan"}
         ],
         "outcome": "partial"
       }
     }'
```

Body: `content` (required), `metadata` (required object: `agent_ref` ULID, `task_id`, `went_well[]`, `gaps[]`, `changes[]` of `{target, change}`, `outcome` ∈ `{success, partial, failure}`), `source_tool` (optional).

Response: `{external_id, is_new}`.

Required scope: `write` on `retrospective`. Cross-agent `agent_ref` references are rejected.

### `GET /api/v1/agent_profiles/<external_id>/checklist`

Resolve an `agent_profile`'s `verification_ref` to the live checklist row. Returns `{checklist: null}` when the profile has no `verification_ref` set or the linked checklist is missing / soft-deleted.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/agent_profiles/01HXY.../checklist' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Response: `{checklist: {external_id, content, agent_ref, trigger, checks[{id, statement, severity}], pass_criteria, captured_at, embed_status, truncated, token_count}}` or `{checklist: null}`.

Required scope: `read` on `verification_checklist`. Cross-agent reads are blocked at the storage layer.

### `POST /api/v1/compiled_artifacts`

Register a compiled artifact (V0.5-P4 / ADR 0019). The AgenticOS Playbook framing: the caller-side compiler reads sources via `GET /api/v1/compile_sources?target=...` (or `recall`), synthesises the narrative, and posts the rendered content here. Tessera stores; the caller compiles.

```bash
curl -s -X POST 'http://127.0.0.1:5710/api/v1/compiled_artifacts' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}" \
     -H 'Content-Type: application/json' \
     -d '{
       "content": "# AgenticOS Playbook\n\nThe digest agent runs weekly and ...",
       "source_facets": ["01HZX1Y2Z3MNPQRSTVWXYZ0123", "01PROJECT00000000000000001"],
       "compiler_version": "claude-opus-4-7",
       "artifact_type": "playbook"
     }'
```

Body: `content` (required, ≤ 65 536 chars), `source_facets` (required array of ULID strings; 1–256 entries), `compiler_version` (required, ≤ 128 chars), `artifact_type` (optional, default `playbook`), `metadata` (optional caller-side dict), `source_tool` (optional).

Response: `{external_id, artifact_type, source_count}`. The pair-write inserts a `compiled_notebook` facet (mode=`write_time`) and a `compiled_artifacts` row sharing the returned `external_id` inside one transaction.

Required scope: `write` on `compiled_notebook`.

### `GET /api/v1/compiled_artifacts/<external_id>`

Fetch one compiled artifact. Returns `{artifact: null}` for missing rows or cross-agent reads (blocked at the storage layer).

```bash
curl -s 'http://127.0.0.1:5710/api/v1/compiled_artifacts/01PLAYBOOK000000000000000A' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Response: `{artifact: {external_id, content, artifact_type, source_facets, compiler_version, compiled_at, is_stale, truncated, token_count}}` or `{artifact: null}`.

Required scope: `read` on `compiled_notebook`.

### `GET /api/v1/compile_sources`

Enumerate source facets tagged `metadata.compile_into = [target]`. Eligible facet types are the ADR 0019 primary inputs (`agent_profile`, `project`, `skill`, `verification_checklist`); other types are filtered out even if tagged.

```bash
curl -s 'http://127.0.0.1:5710/api/v1/compile_sources?target=playbook_main&limit=64' \
     -H "Authorization: Bearer ${TESSERA_TOKEN}"
```

Query params: `target` (required, ≤ 128 chars), `limit` (optional, default 50, max 100).

Response: `{items: [{external_id, facet_type, snippet, captured_at, token_count}], truncated, total_tokens}`.

Required scope: `read` on `compiled_notebook` (so write-scoped callers can pre-read inputs without holding per-source-type read scopes).

## CLI: `tessera playbook`

The `tessera playbook` command tree wraps the storage-only API in `tessera.vault.compiled` so a user can orchestrate compiled-artifact (Playbook) work without touching SQL or hand-writing MCP envelopes. The boundary stays the same: this CLI does not call an LLM. The caller picks an external compiler (Claude Code, a local LLM, manual authoring) and registers the result through `tessera playbook register`.

Each subcommand opens the vault directly (`--vault` / `--passphrase` / `$TESSERA_VAULT` / `$TESSERA_PASSPHRASE`) and resolves the agent id the same way `tessera tokens` and `tessera connect` do — auto-selected when the vault has exactly one agent, otherwise `--agent-id` disambiguates.

| Subcommand | Purpose |
| --- | --- |
| `tessera playbook targets` | Scan `workflow` and `skill` facets for well-formed compile target descriptors (the four required keys `target`, `task`, `artifact_type`, `quality_bar`). Pass `--json` for machine-readable output. |
| `tessera playbook sources <target>` | List source facets tagged `metadata.compile_into = [<target>]`. Mirrors `list_compile_sources` over the vault directly. |
| `tessera playbook scaffold <target> --out <path>` | Write a deterministic Markdown brief covering target, task, source-facet table, required output sections, and provenance expectations. The compiler reads this brief; it never decides for the compiler. Pass `--force` to overwrite an existing file. |
| `tessera playbook register <target> --content <path> --compiler-version <version>` | Pair-write a compiled artifact via `register_compiled_artifact`. Source membership defaults to the `list_for_compilation` enumeration; pass `--source-id <ulid>` (repeatable) to claim explicit sources. `--artifact-type` overrides the descriptor's value when present. |
| `tessera playbook stale` | List artifacts where `is_stale = 1` plus the most recent `compiled_artifact_marked_stale` audit row's `source_external_id` and `source_op` so the user can trace the triggering mutation. |
| `tessera playbook inspect <target_or_ulid> [--field NAME ...] [--provenance] [--require-fresh] [--max-snippet N] [--json]` | Read one artifact, optionally narrowed to one or more fields. Target lookup picks the most recent fresh artifact whose `source_facets` are a non-empty subset of `metadata.compile_into = [<target>]`-tagged facets (including soft-deleted facets so a stale artifact whose source was forgotten still resolves). ULID lookup resolves directly. `--field NAME` matches a Markdown `##`/`###` heading inside the artifact body or a key under `metadata.field_provenance`; missing fields fail loudly with the available section and provenance keys listed once. `--provenance` attaches the matching `field_provenance` entry to each field, or surfaces the full map when no `--field` is given. `--require-fresh` rejects stale artifacts. `--max-snippet` caps each section's snippet (default 400, `0` disables). |

### Example workflow

```bash
# 1. Confirm the compile contract is registered as workflow/skill metadata.
tessera playbook targets --json

# 2. Inspect the source facets that will feed the compile.
tessera playbook sources release_playbook --json

# 3. Emit the compile brief and hand it to an external compiler.
tessera playbook scaffold release_playbook --out /tmp/release.brief.md

# 4. (Outside Tessera) compile the playbook with the chosen runner; write
#    the compiled Markdown to /tmp/release.playbook.md.

# 5. Register the result. Sources default to the compile_into enumeration.
tessera playbook register release_playbook \
    --content /tmp/release.playbook.md \
    --compiler-version "claude-code/release-recipe@2026-05-08"

# 6. After source mutations, list the stale artifacts and re-run step 4.
tessera playbook stale --json

# 7. Read one section of the registered Playbook without loading the full body.
tessera playbook inspect release_playbook \
    --field "Retrieval pipeline" \
    --provenance --require-fresh --json
```

There is intentionally no `tessera playbook compile`. Compilation lives outside the daemon per ADR 0019 §Boundary statement; the CLI scaffolds and registers, the runner compiles. Documented runner workflows — Claude Code, local LLM, and no-LLM manual authoring — live in `docs/playbook-compiler-recipes.md` along with the compiler-version naming convention and the seven minimum artifact sections every recipe in the pack writes. Operator evidence for the four task-shaped targets in `.docs/compiled-playbooks-enhancement-plan.md §Phase 9` accrues under `docs/dogfood/playbook-dogfood.md`; the dogfood gate stays separate from the compiled-notebook artifact-level gate at `docs/dogfood/compiled-notebook-dogfood.md`. Compiled-artifact threats — cross-agent disclosure, provenance spoofing, source-scope leakage through compiled summaries — are catalogued in `docs/threat-model.md §S2.3`.

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
