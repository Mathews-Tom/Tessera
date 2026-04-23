# Changelog

All notable changes to Tessera are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] — v0.1.0-pre

### Benchmark finalisation at 10K facets

- B-RET-1 dataset generator now emits a 10K variant
  (`docs/benchmarks/B-RET-1-swcr-ablation/dataset/s1_10k.json`); the
  harness gains `--dataset` to select it. Fresh 10K fake-adapter run
  recorded: MRR 1.000 for `rerank_only`, 0.970 for `swcr`
  (saturation noise vs. keyword reranker, expected per ADR 0011);
  p95 latency 38.4 ms vs. 40.1 ms (+4.4%, inside the regression
  guard's +15% / +100 ms bound).
- B-RET-2 gains `--n-facets` / `--trials` / `--retrieval-mode`.
  10K fake-adapter baseline: p50 277 ms, p95 284 ms, p99 285 ms —
  inside the v0.1 DoD target of p50 < 500 ms, p95 < 1000 ms.
- B-RET-3 gains `--scale` / `--trials`; scale 5 targets 10K total
  facets across the five v0.1 types. 10K baseline: p50 235 ms,
  p95 240 ms, p99 245 ms — well inside the p50 < 1500 ms / p95 < 3000 ms
  target.
- B-WRITE-1 rebuilt for concurrent writers: 10 threads, 10K preload,
  100 writes each. Aggregate 992 writes/sec, p99 4.4 ms — comfortably
  meeting "≥ 50 writes/sec at p99 < 200 ms".
- B-SEC-1 re-run against a 10K-facet vault with the post-reframe
  `project`/`source_tool` vocabulary. Write p50 overhead 1.41×,
  p95 1.06×; read overhead < 1 (WAL-mode wins at read path). No
  regression vs. the 1K pre-reframe baseline.
- New **B-REEMBED-1** benchmark at
  `docs/benchmarks/B-REEMBED-1-embedder-swap/` — end-to-end
  embedder-rotation wall time. Fake-adapter 10K baseline: 2.06 s
  wall, 4848 facets/s throughput. Pins the storage-side ceiling so
  a future regression in the worker's write path is detected even
  without a live provider. The live-Ollama run for the < 10 min DoD
  target is a P14 hardening task.
- B-EMB-1 re-verified (vocabulary updated: `project` + `source_tool`).
  B-RERANK-1 re-verified against the post-reframe code path — no
  change in shape.

### Observability + diagnostic bundles

- `~/.tessera/events.db` structured event log per
  `docs/determinism-and-observability.md §Structured event log`.
  Plain SQLite (not sqlcipher — no facet content), WAL-mode, 7-day
  rolling retention swept hourly by the daemon.
- `recall_slow` events fired when a `recall` call exceeds the
  configured threshold (default 1500 ms). Payload: retrieval mode,
  facet_types, k, stage breakdown, result count, rerank-degraded and
  truncated flags, source_tool. No query text, no result content.
- Embed-pipeline events (`embed_succeeded`, `embed_failed`,
  `embed_retry_exhausted`) emit on every processed facet. Payload:
  facet_id, model_id, error class name (no error message body).
- `scope_denied` event emitted alongside the audit row whenever an MCP
  call is refused for missing scope.
- `tessera doctor --collect <name>` builds a `.tar.gz` diagnostic
  bundle under the working directory (or `--out-dir`). Contents:
  env.json, config.json, schema.sql, stats.json, recent_events.jsonl,
  retrieval_samples.jsonl, audit_summary.jsonl. Every file passes
  through the scrubber before the tarball is written.
- Scrubber rejects forbidden key names (`*token*`, `*key*`,
  `*passphrase*`, `*secret*`, `*api_*`, `*bearer*`, `*authorization*`),
  strings over 256 characters, and known credential patterns (AWS,
  OpenAI, Anthropic, GitHub PAT, Google API, Slack, Tessera tokens,
  PEM private keys). A scrubber hit aborts bundle creation — the
  tarball is not written.

### Client connectors

- Five MCP client connectors: Claude Desktop, Claude Code, Cursor,
  Codex (TOML), and ChatGPT Developer Mode. `tessera connect <client>`
  mints a capability token, resolves the client's platform-specific
  default config path, and writes the Tessera MCP-server entry with a
  pre-write backup and atomic replace.
- `tessera disconnect <client>` removes the Tessera entry without
  stomping sibling keys the user authored. Missing-file and
  already-absent paths are no-ops.
- ChatGPT Dev Mode ships with an in-daemon one-time-use nonce store
  and `POST /mcp/exchange` endpoint. The CLI mints a session token,
  asks the daemon to stash it under a 192-bit nonce with a 30-second
  TTL, and prints the bootstrap URL the user pastes into ChatGPT. The
  raw token never appears in the URL per ADR 0007.
- New CLI flags: `tessera connect <client> --vault <path> --agent-id N`
  with optional `--url`, `--token-class`, `--path`, `--socket`,
  `--passphrase`. `tessera disconnect <client>` takes `--path` to
  override the default.

### Reframe reconciliation

The April 2026 product reframe shifted Tessera from an agent-identity
substrate-swap layer to a portable context layer for the T-shaped
AI-native user. This release brings the P1–P9 codebase in line with
the post-reframe decision layer ([ADR 0010](docs/adr/0010-five-facet-user-context-model.md),
[ADR 0011](docs/adr/0011-swcr-default-on-cross-facet-coherence.md)).

### Added

- Schema v2 with the five-facet v0.1 vocabulary (`identity`,
  `preference`, `workflow`, `project`, `style`) plus reserved v0.3
  (`person`, `skill`) and v0.5 (`compiled_notebook`) types per ADR
  0010.
- Forward-migration script v1 → v2 that remaps retired facet types
  (`episodic` → `project`, `semantic` → `preference`,
  `relationship` → `person`, `goal` → `project`), drops `judgment`
  rows, introduces the `mode` column, renames `source_client` to
  `source_tool`, and creates the reserved `compiled_artifacts`
  table.
- `forget` MCP tool — soft-delete with an audit entry; replaces the
  retired `assume_identity` slot in the six-tool surface.
- `tessera forget <external_id> [--reason]` CLI passthrough.
- CI grep gates `facet_vocabulary_grep.sh` and `assume_identity_grep.sh`
  blocking reintroduction of retired vocabulary.
- B-RET-3 harness measuring cross-facet `recall(facet_types=all)`
  bundle-assembly latency (replaces the retired `assume_identity`
  latency benchmark).

### Changed

- `retrieval_mode` default flipped from `rerank_only` to `swcr`
  per ADR 0011. The ablation arms remain fully wired.
- `recall` without an explicit `facet_types` filter now fans out
  across every facet type the caller is scoped to read.
- `tessera capture --facet-type` is now required; there is no sensible
  default under the five-facet model.
- B-RET-1 dataset generator emits the five v0.1 facet types at a
  realistic mix (identity 5%, preference 15%, workflow 15%, project
  30%, style 35%).

### Removed

- `assume_identity` MCP tool, the `src/tessera/identity/` module, the
  identity-bundle role map, and all associated audit ops.
- Pre-reframe facet types (`episodic`, `semantic`, `relationship`,
  `goal`, `judgment`) from schema CHECK, scope allowlists, and write
  paths.

### Fixed

- Dispatcher no longer defaults to `("style", "episodic")` on recall;
  it resolves to the caller's scoped-for-read set.

## Released versions

No public releases yet. `v0.1.0` ships when the definition-of-done in
`docs/release-spec.md §v0.1 DoD` is fully green.
