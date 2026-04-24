# Changelog

All notable changes to Tessera are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0rc2] — 2026-04-25 (pre-release, polish)

Release-metadata and repo-ergonomics polish on top of `0.1.0rc1`. No source code changes; the shipped binaries and API surface are identical to rc1.

- **PyPI sdist shrunk from 2.3 MiB to ~100 KiB** by excluding `assets/*.mp4` from the hatch sdist target. The explainer video is a pitch asset, not something setup.py / hatch needs at build time. The wheel is unchanged; only the sdist download channel sees the slim-down. Source-tarball users (`pip download --no-binary :all:` and mirror-builders) get a faster install.
- **Classifier bumped from `Development Status :: 1 - Planning` to `Development Status :: 4 - Beta`.** An RC-stage pre-release with documented performance tiers and a complete feature set is beyond planning. PyPI's project page now reflects the actual maturity.
- **Issue templates added** under `.github/ISSUE_TEMPLATE/`: structured YAML bug-report and feature-request forms, plus a `config.yml` that points first-time filers at `docs/troubleshooting.md` before they open a bug and at `docs/release-spec.md` before they open a feature request.

No other changes. If you're already on `0.1.0rc1` and not reporting bugs, there's no reason to upgrade.

### Install

```bash
pip install --pre tessera-context
# or pin explicitly
pip install tessera-context==0.1.0rc2
```

## [0.1.0rc1] — 2026-04-25 (pre-release)

Tessera v0.1.0 ships the **T-shape cross-facet synthesis demo** end-to-end: capture a user's identity, preference, workflow, project, and style facets in any MCP-capable AI client, then recall them as a coherent cross-facet bundle in a different client. All-local by default (Ollama + sentence-transformers); zero telemetry; sqlcipher-encrypted vault with capability-scoped per-tool access.

### What v0.1 delivers

- **Five-facet capture** across `identity`, `preference`, `workflow`, `project`, `style` via MCP `capture` tool.
- **Cross-facet recall** through `recall(facet_types=all)` with SWCR-weighted coherence as the default retrieval mode (ADR 0011).
- **Soft-delete** via `forget`, with audit trail.
- **Six MCP tools** exposed: `capture`, `recall`, `show`, `list_facets`, `stats`, `forget`.
- **Client connectors** for Claude Desktop, Claude Code, Cursor, Codex (`~/.codex/config.toml`), and ChatGPT Developer Mode.
- **Portable export** via `tessera export --format json|md|sqlite` (+ `tessera import-vault` for JSON round-trip). Exports respect soft-delete via `--include-deleted`.
- **Setup diagnostics** via `tessera doctor` for Ollama / port / sqlite-vec / model / schema / token / facet-type issues.
- **Structured observability** at `~/.tessera/events.db` with `recall_slow`, `embed_backlog`, and `retrieval_rerank_degraded` events; diagnostic-bundle export with content scrubbing.

### Install (all-local mode, the v0.1 default)

```bash
# PyPI (after v0.1.0 tag). Package name is `tessera-context` because
# the short `tessera` PyPI name is held by an abandoned Graphite
# dashboard project (last upload 2017); PEP 541 reclaim is being
# pursued in parallel for a future release. CLI binary and Python
# import path remain `tessera`.
pip install tessera-context
# or from source:
git clone https://github.com/Mathews-Tom/Tessera.git
cd Tessera && uv sync

ollama pull nomic-embed-text

tessera init --vault ~/.tessera/vault.db
tessera daemon start --vault ~/.tessera/vault.db
tessera connect claude-desktop --vault ~/.tessera/vault.db
```

Full T-shape demo walkthrough: `docs/pitch.md` and `docs/release-spec.md §v0.1 DoD`. Architecture deep-dive: `docs/system-design.md`.

### Performance tiers (measured)

Real adapters (Ollama `nomic-embed-text` + sentence-transformers `cross-encoder/ms-marco-MiniLM-L-6-v2`), `rerank_candidate_limit=20`, 100 trials, reference hardware baseline (MacBook Pro M1 Pro 10-core CPU / 16-core GPU, 16 GB RAM, macOS 15.x, daemon idle except for the test query, Ollama model pinned via `keep_alive=-1`).

| Tier | Vault size | p50 | p95 | p99 | Evidence |
|------|-----------:|----:|----:|----:|----------|
| Demo-day | ≤ 500 facets | 404 ms | 574 ms | 674 ms | `docs/benchmarks/B-RET-2-recall-latency/results/20260423T215936Z.json` |
| Steady-state (CPU reranker) | 10K facets | 730 ms | 778 ms | 897 ms | `.../20260423T182517Z.json` |
| Steady-state (MPS reranker, opt-in) | 10K facets | 710 ms | 832 ms | — | `.../20260423T212745Z.json` |

Re-embed at 10K facets: 442.7 s wall / 22.6 facets/s throughput (`docs/benchmarks/B-REEMBED-1-embedder-swap/`). Concurrent capture: 992 writes/sec at p99 4.4 ms (`docs/benchmarks/B-WRITE-1-concurrent-capture/`).

### Security surface

- **Encryption at rest** via sqlcipher + argon2id-derived key.
- **Capability tokens** per-tool / per-scope / per-facet-type; stored as salted sha256; per-request re-validation against `revoked_at`; 30-minute access TTL for session-class tokens.
- **CSRF protection** on HTTP MCP via Origin-header allowlist.
- **Zero outbound network** by default: CI `no-outbound` job blocks every non-loopback destination on the full test suite; cloud adapters (OpenAI embedder, Cohere reranker) require explicit import.
- **OS keyring** (Keychain / secret-service / Credential Manager) is the only source for cloud adapter API keys. Env-var fallback is refused loudly.
- **Audit log** with closed payload allowlist; no facet content, query text, or token values ever cross the boundary.
- Full threat-model coverage map: `docs/threat-model-coverage.md`.

### Determinism

- Retrieval pipeline produces bit-identical results across runs on the CPU reranker backend with the same seed on the same vault state.
- MPS / CUDA backends are bit-identical within a single daemon lifetime on same hardware. `TESSERA_RERANK_DEVICE=cpu` forces CPU for cross-run replay testing.
- Full spec: `docs/determinism-and-observability.md`.

### Known limitations (v0.1)

- **No people or skill facets.** Deferred to v0.3 per ADR 0010.
- **No importers** for ChatGPT or Claude conversation history. Deferred to v0.3.
- **No write-time compilation** or episodic temporal retrieval. Deferred to v0.5.
- **Linear scan dense vector search.** Acceptable to ~100K facets per ADR 0002; ANN index is post-v0.1 work.
- **CUDA reranker path shipped but unmeasured** — auto-detection priority is CUDA > MPS > CPU; no CUDA hardware has been benchmarked yet. The code path reuses sentence-transformers' existing CUDA integration, so the determinism and correctness story is the same as MPS.
- **HMAC-chained audit log** is v0.3 scope. v0.1 audit integrity relies on vault encryption-at-rest to make tampering detectable via passphrase loss, not a cryptographic chain.
- **Dependency CVE scanning** is manual via `uv lock` review. Automated `pip-audit` in CI is v0.1.x follow-up.
- **stdio MCP bridge** lands in v0.1 as `tessera stdio`, used by Claude Desktop. Speaks canonical MCP JSON-RPC 2.0 on the stdio side and translates to Tessera's custom HTTP envelope. No external bridge (`mcp-remote` / `mcp-proxy`) required.
- **ChatGPT Developer Mode integration deferred to v0.1.x.** Three stacked blockers: (a) `http://127.0.0.1:...` rejected as "Unsafe URL" — needs HTTPS front, (b) no Bearer auth mode in the "New App" dialog dropdown (only OAuth / Mixed / No Auth), (c) the same protocol-shape mismatch `tessera stdio` solves for stdio needs a server-side HTTP equivalent. Workaround for v0.1: use **Claude Code** as the second client on the recall side. Two Anthropic clients sharing one vault still demonstrates the "portable context" story.

### What v0.1 explicitly does NOT ship

Per `docs/non-goals.md`: no auto-capture, no AI-generated capture, no hosted-only mode, no model reselling, no telemetry, no cloud-PaaS default dependency. See `docs/release-spec.md §What v0.1 explicitly does NOT ship` for the full list.

### Blockers before v0.1.0 is tagged

- Real-user test: one external engineer completes the T-shape demo unaided, recorded. P14 task 6.
- Cross-platform smoke test: clean install + demo on macOS + Ubuntu + Windows, recorded. P14 task 4.

---

## [Unreleased] — v0.1.0-pre

### P14 pre-release hardening

- **`rerank_candidate_limit=20`** is the production default on the retrieval pipeline. The B-RET-2 sweep (six result files under `docs/benchmarks/B-RET-2-recall-latency/results/`) showed the knee of the latency curve at k=20; B-RET-1 at k=20 confirmed no quality regression (MRR/nDCG/purity saturate at 1.000 across all three arms on the 2K dataset). See PR #17.
- **Reranker device auto-detection** (CPU/MPS/CUDA) via `tessera.adapters.devices.detect_best_device`. `TESSERA_RERANK_DEVICE=cpu` forces CPU for cross-run bit-identical determinism. Resolved device is audited at daemon startup via the new `daemon_warmed` audit op.
- **Ollama model warm-keep** — every `/api/embeddings` POST carries `keep_alive=-1`, pinning the embedding model for the lifetime of the Ollama daemon. Without this, real-user recalls after idle paid a 2–5 s cold-load penalty invisible to the benchmark.
- **Explicit daemon warm-up** at supervisor startup: the embedder and reranker both load before the control socket opens, shifting the cold-load cost off the first user recall.
- **v0.1 DoD revised** in `docs/release-spec.md` with a tiered latency table backed by committed benchmark artifacts; original single-number gate conflated demo-day and year-two steady-state conditions.
- **Tessera export** (`tessera export --format json|md|sqlite`) + `tessera import-vault` — closes the P14 data-portability DoD item. JSON is byte-equivalent round-trippable; Markdown is per-facet-type; SQLite is a plain-text decrypted copy. Seven integration tests cover round-trip fidelity and `--include-deleted` semantics.
- **Threat-model coverage audit** at `docs/threat-model-coverage.md` — every `v0.1`-tagged mitigation in `docs/threat-model.md` mapped to a test path or enforcing code reference, plus OWASP MCP-over-HTTP self-audit. Three follow-ups recorded for v0.1.x (socket-mode assertion, `pip-audit` automation, HMAC chain is explicitly v0.3 scope).

### Benchmark finalisation — live Ollama reruns

### Benchmark finalisation — live Ollama reruns

Real-adapter reruns against Ollama `nomic-embed-text` (768 dim) +
sentence-transformers `cross-encoder/ms-marco-MiniLM-L-6-v2` on the
reference hardware baseline (MacBook Pro M1 Pro, 16 GB RAM, macOS
15.x, no concurrent Ollama workload).

- B-RET-1 @ 2K live: MRR both arms saturated at 1.000;
  p95 `rerank_only` 1078 ms, `swcr` 1183 ms — +105 ms / +9.7%,
  inside the +15% / +100 ms regression-guard bound.
- **B-RET-2 @ 10K live: p50 1094 ms, p95 1154 ms, p99 1514 ms.**
  Exceeds the v0.1 DoD ceiling (p50 < 500 ms, p95 < 1000 ms). The
  CPU MiniLM cross-encoder rerank on 50 candidates is the dominant
  cost; Ollama embed for a single query is ~20–50 ms. DoD target
  needs re-calibration against real-adapter costs (P14 decision
  point: revise target, shrink candidate count, make reranker
  optional on slow hardware, or accept the number and document it
  in release notes).
- **B-RET-3 @ 10K live: p50 2110 ms, p95 2316 ms, p99 2395 ms.**
  p50 exceeds 1500 ms; p95/p99 inside 3000 ms. Same reranker
  bottleneck, compounded by five-facet-type fan-out.
- **B-REEMBED-1 @ 10K live: 442.7 s (7.4 min) wall, 22.6 facets/s
  throughput. Inside the < 10 min DoD ceiling.** The storage-side
  2 s ceiling from the fake-adapter run bounds the theoretical
  minimum; the rest is Ollama embedding throughput.

Live reruns are committed alongside the fake-adapter 10K results;
DoD reconciliation lands in P14.

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
