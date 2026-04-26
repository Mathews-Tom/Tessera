# Tessera — Determinism and Observability

**Status:** Draft 1
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## Why this document exists

Tessera holds two commitments that look contradictory:

1. **No telemetry.** Verified by CI grep, enforced by anti-roadmap.
2. **Solo developer supporting a growing user base.** By v1.0, hundreds of active vaults, one maintainer, dissertation in parallel.

Without explicit design, the first commitment makes the second impossible: bug reports become irreproducible, performance regressions invisible, coherence drift unfalsifiable. This document specifies the minimum machinery for debuggability that does not cross into telemetry.

Two principles:

- **Reproducible by default.** Given a query and a vault state, retrieval must produce the same result on a replay.
- **Diagnosable on request.** Users can produce a redacted bundle describing what went wrong, entirely locally, uploaded only if they choose.

## Determinism

### Retrieval pipeline determinism

The retrieval pipeline (system-design.md §Retrieval pipeline) has five stages. Each must be deterministic given the same inputs and seed.

| Stage                     | Deterministic?          | Seeding                                                       |
| ------------------------- | ----------------------- | ------------------------------------------------------------- |
| BM25 (FTS5)               | Yes                     | None required; FTS5 is deterministic                          |
| Dense search (sqlite-vec) | Yes                     | None required; cosine is deterministic                        |
| RRF fusion                | Yes                     | Tie-break on `facets.id` ascending                            |
| Cross-encoder rerank      | Tier-dependent; see *Device-tier determinism* below | `torch.manual_seed(seed)` per call (scoped RNG; global `torch.use_deterministic_algorithms(True)` is not set because some arm64 torch builds SIGBUS on non-deterministic fallback ops — see `src/tessera/adapters/st_reranker.py`). The seeded RNG covers every sampling point inside `CrossEncoder.predict` on the CPU path. |
| SWCR reweighting          | Yes                     | Closed-form; no sampling                                      |
| MMR diversification       | Yes                     | Greedy with deterministic tie-break on `facets.id`            |
| Token budget enforcement  | Yes                     | Integer token counts                                          |

### Execution-provider determinism

Both embedder and reranker run through fastembed via ONNX Runtime. Provider selection happens inside fastembed (CPU / CoreML / CUDA) at session creation; the daemon does not configure it explicitly. The determinism guarantee varies by provider:

| Execution provider | Bit-identical across runs? | Notes |
|--------------------|----------------------------|-------|
| CPU | Yes | The integration tests run the embed + rerank path through ONNX Runtime's CPU provider and assert bit-identical rerun of the same query. This is the strong guarantee and the CI baseline. |
| CoreML (Apple Silicon) | Within a single daemon lifetime on same hardware | Metal float-op ordering can vary across process launches on some macOS revisions; results are stable within one daemon process. |
| CUDA | Within a single daemon lifetime on same hardware | ONNX Runtime's CUDA provider picks fastest-available kernels by default; the CPU provider remains the cross-run reproducibility ground truth. |

For users who need cross-run bit-identity (audit-log replay testing, reproducibility studies), pinning fastembed to the CPU provider is the supported path. The `daemon_warmed` audit row records the active execution provider at startup so operators can verify which tier the daemon is running on after the fact.

### Seed source

The seed for a retrieval call is:

```
seed = sha256(query_text || vault_id || active_embedding_model_id || retrieval_config_hash)[0:8]
```

Same query on the same vault state returns bit-identical results. Changing any of the four inputs changes the seed. The `retrieval_config_hash` captures the current SWCR parameters, rerank model revision, and MMR λ; tuning parameters intentionally invalidates the seed.

### `deterministic: bool` flag

MCP tools that run retrieval accept an optional `deterministic: bool = true`. Setting it to `false` enables:

- Random tie-breaking in RRF (seeded per call, not per query+vault).
- Stochastic candidate sampling above the top-M cutoff.

Use cases for `deterministic: false` are advanced (explore-exploit tradeoffs, A/B bundling). Default is always `true`.

### Audit log fields for replay

Every `recall` entry in `audit_log` records enough to reproduce:

```json
{
  "op": "recall",
  "query_hash": "sha256(query_text)",
  "seed": "0x1a2b3c4d5e6f7890",
  "params": {
    "k": 5,
    "facet_types": ["identity", "preference", "workflow", "project", "style"],
    "deterministic": true
  },
  "active_embedding_model_id": 2,
  "retrieval_config_hash": "sha256(...)",
  "result_facet_ids": ["01H...Z", "01H...Y"],
  "duration_ms": 312,
  "rerank_degraded": false,
  "source_tool": "chatgpt-devmode"
}
```

The `query_text` itself is not logged by default (may contain sensitive content). The `query_hash` plus the `result_facet_ids` plus the seed is sufficient for the user to replay: `tessera recall --replay-from-audit <audit_id>` reconstructs inputs from the user's own query history (or the user manually re-enters the query).

## Observability — local-only by design

All observability surfaces write to the local vault or local log files. No outbound calls, ever.

### Structured event log

A separate SQLite file `~/.tessera/events.db` captures structured events for operational debugging:

```sql
CREATE TABLE events (
  id         INTEGER PRIMARY KEY,
  at         INTEGER NOT NULL,
  level      TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warn', 'error')),
  category   TEXT NOT NULL,                   -- 'retrieval', 'embed', 'auth', 'migration', ...
  event      TEXT NOT NULL,                   -- 'recall_slow', 'embed_failed', 'token_revoked', ...
  attrs      TEXT NOT NULL DEFAULT '{}',      -- JSON, bounded size, no facet content
  duration_ms INTEGER,
  correlation_id TEXT                         -- per-MCP-call ULID
);

CREATE INDEX events_at    ON events(at DESC);
CREATE INDEX events_cat   ON events(category, level, at DESC);
```

Events are separate from the audit log. Audit log is legal-grade record of mutations. Events are operational telemetry: slow queries, embedder timeouts, rerank degradations, capability mismatches.

### Retention

- Audit log: unbounded (see `system-design.md §Storage` and rotation policy).
- Events: rolling 7 days by default; configurable. Older events are dropped on a daily sweep.

### Slow-query sampling

Retrieval calls that exceed a configurable latency threshold emit a `recall_slow` event with:

- `seed`, `params`, `duration_ms`, `stage_breakdown_ms` (per-stage timing), `candidate_counts_per_stage`.
- **No query text, no result content.** Only what is needed to reproduce on the user's own vault.

Default threshold: p99 baseline + 50%. User-tunable.

### Embed-pipeline events

- `embed_enqueued`, `embed_succeeded`, `embed_failed` with error classification (network, OOM, adapter-specific).
- `embed_retry_exhausted` when a facet hits the retry cap.
- `reembed_started`, `reembed_progress`, `reembed_completed` for re-embedding passes.

### Capability events

- `token_issued`, `token_refreshed`, `token_revoked`, `auth_denied`, `scope_denied`.
- `auth_denied` + `scope_denied` include the requested operation and scope, not the presented token content.

## Diagnostic bundles (opt-in, user-initiated)

`tessera doctor --collect <bundle-name>` produces a single tarball the user can inspect and, if they choose, attach to a bug report. The bundle is built locally; nothing is uploaded by Tessera itself.

Contents:

| File                      | Purpose                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------ |
| `env.json`                | OS, kernel, CPU, RAM, Tessera version, Python version, fastembed version, ONNX Runtime version, active models |
| `config.yaml` (redacted)  | User config with secrets scrubbed (keyring references kept, values removed)          |
| `schema.sql`              | `.schema` dump of the vault — no content                                             |
| `stats.json`              | Output of `tessera stats`                                                            |
| `recent_events.jsonl`     | Last N events from `events.db` at `info` level and above, no content payload         |
| `retrieval_samples.jsonl` | Last 10 slow-query events with seeds, params, stage breakdowns, no content           |
| `audit_summary.jsonl`     | Counts per operation type per day for the last 30 days                               |

**Excluded**: facet content, query text, embedding vectors, token values, passphrase, OS keyring entries, API keys.

### Scrubber

A single `scrub.py` runs over the bundle before the tarball is finalized, asserting:

- No field whose JSON key matches `*token*`, `*key*`, `*passphrase*`, `*secret*`, `*api_*`.
- No string longer than 256 characters (content escape hatch).
- No field matching common credential regex (AWS, OpenAI, etc.).

Any assertion failure aborts bundle creation with a clear error. The bundle is considered safe to share only after it passes scrubber assertions.

### Review before share

`tessera doctor --collect` prints the bundle path and explicitly instructs the user to open and review the tarball before sharing. Tessera does not auto-upload.

## CI enforcement

Three CI jobs enforce the commitments:

1. **No-outbound test**: run the full test suite with `iptables -A OUTPUT -d ! 127.0.0.1 -j REJECT` (Linux CI). Tests pass only if all required calls go to the expected adapters. Failure = some dependency made a hidden outbound call.
2. **No-telemetry grep**: reject PRs that add imports of `requests`, `httpx`, `aiohttp`, `urllib.request` outside `src/tessera/adapters/`. Adapters have their own allowlist. Three non-adapter files are also allowlisted by exact path: `src/tessera/cli/_http.py` (shared CLI loopback client to `tesserad` at `127.0.0.1`, used by every subcommand that calls an MCP tool by name; extracted from `cli/tools_cmd.py` in the v0.3 People + Skills refactor so the `httpx` import lives in exactly one place), `src/tessera/daemon/stdio_bridge.py` (stdio-to-HTTP bridge that Claude Desktop launches; every `tools/list` and `tools/call` POSTs to `http://127.0.0.1:<port>/mcp`, the same loopback path as `cli/_http.py`), and `src/tessera/cli/curl_cmd.py` (`tessera curl <verb>` recipe builder for the `/api/v1/*` REST surface; executes the printed curl recipe via `httpx` against `$TESSERA_DAEMON_URL` — default `http://127.0.0.1:5710` — so users can verify the recipe before wiring it into a hook script; `--print` mode skips the HTTP call entirely). All three are user-initiated, bounded, and reach only localhost. Extending this list requires the same-commit update in `scripts/no_telemetry_grep.sh`.
3. **Determinism test**: run 100 `recall` calls with the same query on the same seeded vault; assert bit-identical results.

## What this is NOT

- **Not telemetry.** No network calls, ever. `events.db` is user-owned.
- **Not a crash reporter.** Users report crashes by running `tessera doctor --collect` and attaching the bundle.
- **Not a performance monitor.** Events accumulate locally; if the user wants dashboards, they can query `events.db` with any SQLite client.
- **Not an analytics pipeline.** No counts, no funnels, no cohort tracking.

## Revisit triggers

- A user reports a bug that the diagnostic bundle cannot reproduce. Expand bundle contents to cover the gap.
- `events.db` growth becomes a user complaint. Tighten retention defaults.
- A scrubber miss ships in a bundle. Audit scrubber coverage and add regression test.
