# Tessera Development Plan

**Status:** Canonical forward plan replacing the prior `.docs/*.md` enhancement, execution, and handoff documents.
**Date:** 2026-05-03
**Scope:** Remaining work only. Completed phase history stays in `CHANGELOG.md`, `docs/adr/`, `docs/release-spec.md`, benchmark result files, and git history.

This document supersedes and absorbs the remaining actionable content from:

- `.docs/animocerebro-exchange-analysis.md`
- `.docs/animocerebro-followup-development-plan.md`
- `.docs/animocerebro-followup-execution-prompt.md`
- `.docs/deliberate-capture-recall-enhancement-plan.md`
- `.docs/development-plan.md`
- `.docs/execution-prompt.md`
- `.docs/handoff.md`

After this file is reviewed, those seven documents can be removed.

## Current State

The codebase has completed the major technical v0.5 work:

- P10-P14, v0.3, and v0.4 shipped on the rc track.
- AnimoCerebro Phase A1 is complete: recall honesty uses `degraded_reason`.
- AnimoCerebro Phase A2 is complete: cross-facet vs. cross-session boundaries and plugin non-goals are documented.
- AnimoCerebro Phase B is complete: ADR 0015 chose to keep Jaccard for person/skill coherence.
- AnimoCerebro Phase C is intentionally skipped because ADR 0015 chose Option C.
- AnimoCerebro Phase D is complete through ADR 0021, audit-chain migration, `audit_log_append`, `tessera audit verify`, and security tests.
- V0.5-P0/P1/P2/P3/P4/P5/P6/P7/P8/P9 part 1/P9b are technically complete.
- V0.5-P9c row-merge and V0.5-P10 episodic temporal retrieval remain conditional, signal-driven work.

Last verified locally:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/tessera tests
uv run pytest -m "not slow" --no-cov
uv run pytest -m "not slow" --cov=tessera --cov-branch --cov-report=term-missing --cov-fail-under=80
```

The latest observed test suite was 1149 passing tests with 84.40% total coverage.

## Immediate Decision

Default path: **tag `v0.5.0rc1` after regression benchmarks pass**.

Reason: all technical v0.5-rc1 commitments are implemented. The remaining gates are dogfood, load, external-user, or release-validation work. Do not block rc1 on speculative row-merge or episodic temporal retrieval.

Alternatives:

| Option | Decision | Rationale |
| --- | --- | --- |
| Tag v0.5.0rc1 | Recommended | Technical commitments are complete; dogfood starts after rc1. |
| Build V0.5-P9c row-merge before rc1 | Defer | Snapshot sync satisfies v0.5. Row-merge needs real multi-device conflict cases. |
| Build V0.5-P10 episodic temporal retrieval before rc1 | Defer | Stretch work. Needs dogfood signal on temporal queries. |
| Build repo-local project-context graph before rc1 | Defer | Valuable, but not part of v0.5. It is post-GA UX/integration work over existing vault primitives. |

## Release Track

### R1 — v0.5.0rc1 Release Prep

**Goal:** Produce a taggable v0.5.0rc1.

Tasks:

- [ ] Run B-RET-1 on `main` and compare against latest baseline.
- [ ] Run B-RET-2 on `main` and compare against latest baseline.
- [ ] Run B-RET-3 on `main` and compare against latest baseline.
- [ ] Run B-SEC-1 on `main` and compare against latest baseline.
- [ ] Confirm no regression against the release-spec v0.5 claims.
- [ ] Update `docs/release-spec.md §v0.5` checkboxes to match implemented audit-chain and sync state.
- [ ] Move `CHANGELOG.md` `[Unreleased]` v0.5 entries under `[0.5.0rc1] — 2026-MM-DD`.
- [ ] Bump `pyproject.toml` version to `0.5.0rc1`.
- [ ] Run full validation gates.
- [ ] Tag `v0.5.0rc1` and push the tag.
- [ ] Open tracking issues for the post-rc gates:
  - 50K facet S3 sync load test
  - 30-day multi-machine sync dogfood
  - 60-day dissertation compiled-notebook dogfood
  - external user T-shape demo
  - cross-platform clean install smoke

Exit gate:

- [ ] `git status --short` is clean.
- [ ] Benchmark results are committed under `docs/benchmarks/*/results/`.
- [ ] Full validation passes.
- [ ] `CHANGELOG.md`, `pyproject.toml`, and `docs/release-spec.md` agree on rc1 state.

### R2 — v0.5 Dogfood Gates

**Goal:** Convert rc1 from technically complete to empirically validated.

Tasks:

- [ ] Use Tom's dissertation research topic as a real `compiled_notebook`.
- [ ] Dogfood write-time compilation for 60+ days.
- [ ] Verify compiled output is genuinely useful, not only syntactically valid.
- [ ] Confirm compilation is idempotent and resumable under interrupted runs.
- [ ] Run multi-machine sync continuously for 30+ days with at least one real user or real second-machine workflow.
- [ ] Run S3 sync with 50K+ facets without blocking the daemon.
- [ ] Record observed p50/p95/p99 sync times and daemon responsiveness.
- [ ] Verify post-pull `tessera audit verify` on restored vaults.
- [ ] Document any real conflict cases before deciding on row-merge.

Exit gate:

- [ ] 60-day compiled-notebook dogfood completed and documented.
- [ ] 30-day multi-machine sync dogfood completed and documented.
- [ ] 50K-facet sync load result committed.
- [ ] No P0/P1 data-loss, audit-chain, or sync-integrity bugs remain open.

### R3 — External Validation / GA Readiness

**Goal:** Close the remaining release-spec gates that require humans or clean machines.

Tasks:

- [ ] Record clean install smoke on macOS:
  - install
  - init
  - activate model
  - start daemon
  - connect client
  - capture
  - recall
- [ ] Record clean install smoke on Ubuntu.
- [ ] Record clean install smoke on Windows.
- [ ] Verify v2 -> v3 migration on a real rc2 vault on each platform.
- [ ] Verify v3 -> v4 migration on a populated real vault.
- [ ] Have one external user complete the T-shape demo unaided and record the session.
- [ ] Have 5+ external users complete the T-shape demo and share feedback.
- [ ] Measure setup time for a non-developer technical user; target < 15 minutes excluding model download.
- [ ] Verify 3+ AI tool clients against real configs, including Claude Code and Cursor. ChatGPT Developer Mode remains gated by HTTPS/auth compatibility.
- [ ] Add `CONTRIBUTING.md`.
- [ ] Add `SECURITY.md` or a clear vulnerability-reporting section.
- [ ] Decide whether the public GA version is `0.5.0` or a later rc.

Exit gate:

- [ ] Cross-platform smoke evidence exists.
- [ ] External user demo evidence exists.
- [ ] Migration evidence exists.
- [ ] Public repo has contributor and security-reporting guidance.

## Conditional Engineering Work

### E1 — V0.5-P9c Multi-Device Row Merge

**Trigger:** real dogfood shows snapshot sync causes unacceptable lost work or merge friction.

Do not build this before rc1 without a concrete conflict case.

Scope:

- [ ] Detect divergence between local and remote manifests.
- [ ] Compare two SQLCipher vault snapshots at row granularity.
- [ ] Append non-conflicting remote facets into the local vault using `content_hash` dedup.
- [ ] Preserve audit-chain integrity during merge.
- [ ] Define conflict semantics for:
  - people
  - agent profiles
  - compiled artifacts
  - automation registry rows
  - sync watermark state
- [ ] Add `tessera sync conflicts`.
- [ ] Add a manual conflict-resolution workflow.
- [ ] Add tests for simultaneous push, replayed manifests, cross-vault overwrite, and conflicting entity rows.

Hard boundaries:

- No silent row overwrite.
- No best-effort merge that breaks audit-chain verification.
- No auto-merge for semantic entities without an inspectable conflict record.

### E2 — V0.5-P10 Episodic Temporal Retrieval

**Trigger:** real dogfood repeatedly asks temporal questions such as "what was I thinking about this project two weeks ago?"

Scope:

- [ ] Define episode segmentation over `project` and session-volatility facets.
- [ ] Add temporal query parameters to recall or a dedicated temporal recall surface.
- [ ] Add time-range filtering that composes with SWCR, MMR, and budget enforcement.
- [ ] Add benchmark cases for stale project context and cross-session continuity.
- [ ] Update docs to distinguish:
  - cross-facet coherence
  - cross-tool portability
  - cross-session temporal coherence

Hard boundaries:

- Do not claim cross-session coherence from ordinary SWCR.
- Do not auto-summarize sessions inside the daemon.
- Keep write-time synthesis caller-owned.

## Project Context Layer

These items come from the markdown-codebase-graph analysis. The core lesson is narrow: Tessera should not become a markdown knowledge graph, but it should learn from enforced, navigable, source-linked project-context workflows.

Treat this as post-v0.5 / v0.6 candidate work. It should land before the broader memory-policy work if no row-merge or temporal-retrieval trigger is active, because it directly improves Tessera's daily use in codebases without weakening the single encrypted vault model.

### K1 — Repo-Local Project Context Adapter

**Goal:** let a repository carry inspectable markdown context that syncs into Tessera facets.

Shape:

- Optional directory: `.tessera/context.md/` or `tessera.md/`.
- Markdown sections become disk-backed facets, not a replacement for the vault.
- Section IDs are stable handles for explicit reference, display, validation, and sync.

Candidate mapping:

| Markdown content | Tessera facet |
| --- | --- |
| architecture/design notes | `project` |
| coding procedures | `workflow` or `skill` |
| test/spec obligations | `verification_checklist` |
| agent operating notes | `agent_profile` or `project` |
| synthesized deep docs | `compiled_notebook` |

Tasks:

- [ ] Decide directory name and section ID format.
- [ ] Generalize the existing skill `disk_path` pattern to disk-backed project-context facets without breaking skill sync.
- [ ] Add direct-vault CLI commands for sync:
  - `tessera context sync-from-disk`
  - `tessera context sync-to-disk`
  - `tessera context list`
  - `tessera context show`
- [ ] Store file path, section ID, source hash, and last sync timestamp in metadata or first-class columns.
- [ ] Keep sync deterministic and fail-loud on duplicate section IDs.
- [ ] Add tests for idempotent import, modified section update, deleted section tombstone, duplicate section rejection, and skill-sync coexistence.

Boundary:

- Markdown is an authoring and review surface. The encrypted SQLite vault remains the source of truth for retrieval, auth, sync, audit, and cross-tool access.

### K2 — Source Reference Index

**Goal:** tie project-context facets to implementation symbols and source-line backlinks.

Tasks:

- [ ] Define a structured `source_refs` metadata shape with `path`, optional `symbol`, optional `line`, and `ref_kind`.
- [ ] Support explicit source comments such as:
  - `# @tessera: [[project#Retrieval Pipeline]]`
  - `// @tessera: [[skill#Release Checklist]]`
- [ ] Add scanner support for Python, TypeScript/JavaScript, Go, Rust, and C-family source files as demand warrants.
- [ ] Prefer `rg` for comment discovery and keep language-specific symbol parsing out of the daemon hot path.
- [ ] Add `tessera context refs <target>` to show:
  - facets/sections referencing a source symbol
  - source comments referencing a facet/section
  - snippets around each backlink
- [ ] Add tests for valid refs, broken refs, ambiguous refs, unsupported extensions, and source comments inside ignored directories.

Boundary:

- Source scanning is CLI/check-time work. The daemon stores indexed references but does not parse arbitrary source files during recall.

### K3 — Context Integrity Check

**Goal:** make project-context drift a failing check instead of a manual review burden.

Tasks:

- [ ] Add `tessera check context`.
- [ ] Validate broken wiki links between disk-backed sections.
- [ ] Validate ambiguous short references and suggest fully qualified fixes.
- [ ] Validate source comments pointing at missing facets/sections.
- [ ] Validate `require-code-mention` style obligations for verification-checklist sections.
- [ ] Validate stale disk-backed facets where file hash and vault metadata diverge.
- [ ] Validate stale compiled notebooks when source facets changed.
- [ ] Validate missing or overlong leading summaries for disk-backed sections.
- [ ] Return machine-readable JSON for hooks and human-readable markdown for terminals.

Boundary:

- Checks diagnose and fail. They do not auto-edit source files, auto-delete facets, or fabricate missing docs.

### K4 — Explicit Context Expansion

**Goal:** give users and agents deterministic handles alongside semantic recall.

Tasks:

- [ ] Add `tessera expand <text>` and a matching MCP/REST surface.
- [ ] Resolve `[[...]]` references against:
  - facet external IDs
  - skill names
  - people aliases
  - disk-backed project-context section IDs
  - compiled-artifact IDs
- [ ] Return the rewritten text plus a bounded context block containing resolved IDs, summaries, source locations, and warnings.
- [ ] Reject unresolved or ambiguous refs instead of silently dropping them.
- [ ] Add tests for exact, short, ambiguous, missing, and permission-denied references.

Boundary:

- Expansion is not a replacement for recall. It is an explicit-reference primitive for prompts, hooks, and review workflows.

### K5 — Agent Hook Scaffolding

**Goal:** wire Tessera into agent lifecycle hooks without making Tessera an agent runtime.

Tasks:

- [ ] Extend `tessera connect <client>` with an opt-in `--hooks` flag where the client supports hooks.
- [ ] Prompt-start hook:
  - expands explicit `[[...]]` refs
  - runs bounded recall on the user's intent
  - injects context with source IDs and warnings
- [ ] Stop/end hook:
  - runs `tessera check context`
  - warns or blocks when project-context files and code changes drift
  - never fabricates evidence or auto-commits fixes
- [ ] Preserve existing non-Tessera hooks during config writes.
- [ ] Add client-specific support only where reliable hook semantics exist.

Boundary:

- Tessera stores, retrieves, checks, and wires hooks. The agent runtime decides how to use the injected context and how to repair drift.

## Memory Policy Enhancements

These come from `deliberate-capture-recall-enhancement-plan.md`. They are not core storage requirements for v0.5-rc1. Treat them as agent-runtime and UX work above Tessera's capture/recall primitives.

### M1 — Capture Suggestions

**Goal:** agents propose durable memories instead of silently persisting everything.

Tasks:

- [ ] Define a memory candidate schema with:
  - scope
  - confidence
  - durability
  - sensitivity
  - source
  - TTL
  - capture reason
  - policy score
- [ ] Add an API for capture suggestions without automatic persistence.
- [ ] Support approve, edit, reject, pin, assign scope, assign TTL, and mark sensitivity.
- [ ] Store rejection feedback as policy-tuning signal where appropriate.
- [ ] Add tests for secret rejection, transient-log rejection, explicit-preference acceptance, and duplicate suppression.

Boundary:

- Tessera stores and reviews. Agent runtimes own policy thresholds.

### M2 — Recall Trace

**Goal:** make memory use inspectable.

Tasks:

- [ ] Record trace metadata for recall calls:
  - generated query
  - retrieved memories
  - injected memories
  - filtering reasons
  - ranking reasons
  - scope applied
  - timestamp
  - agent identity
- [ ] Ensure traces exclude secrets, token values, embeddings, and long content.
- [ ] Add CLI or diagnostic-bundle access to recent recall traces.
- [ ] Add tests proving trace payloads pass scrubber rules.

Boundary:

- Trace explains memory use. It must not become telemetry.

### M3 — Scope-Aware Memory Policy

**Goal:** scope participates in capture, retrieval, and injection.

Candidate scopes:

- `global`
- `user`
- `workspace`
- `repo`
- `project`
- `session`

Tasks:

- [ ] Decide whether these scopes live in facet metadata, first-class schema columns, or a separate policy layer.
- [ ] Add filtering rules so repo-scoped memory does not leak into unrelated projects.
- [ ] Add promotion path from session memory to durable memory.
- [ ] Add tests for cross-repo isolation.

Boundary:

- Do not conflate auth scopes with memory relevance scopes.

### M4 — Conflict Handling

**Goal:** contradictory memories are visible and resolvable.

Rules:

- Current user instruction wins over memory.
- Observed repo state wins over stale memory.
- Newer explicit user correction wins over older inferred memory.
- Pinned memory wins over ordinary memory unless contradicted by current instruction.
- Stale memories are marked stale, not silently deleted.

Tasks:

- [ ] Add conflict detection fixtures.
- [ ] Add stale-memory marking for policy-level conflicts.
- [ ] Add review UX or CLI for conflicts.
- [ ] Track `supersedes` and `superseded_by` relationships.

### M5 — Memory Quality Controls

**Goal:** prevent recall from degrading into noisy context stuffing.

Metadata candidates:

- confidence
- source
- created_at
- last_used_at
- usage_count
- TTL
- sensitivity
- scope
- supersedes / superseded_by

Tasks:

- [ ] Decide which fields belong in core schema vs. metadata.
- [ ] Add ranking penalties for stale, low-confidence, or low-usage memories.
- [ ] Add evals for recall precision, conflict rate, staleness rate, and user correction rate.

### M6 — Automatic Capture / Recall Evaluation

Tasks:

- [ ] Build repo-onboarding eval requiring stored conventions.
- [ ] Build implementation-task eval requiring stored preference.
- [ ] Build design-task eval requiring prior architectural decision.
- [ ] Build conflict eval where current instruction overrides memory.
- [ ] Build sensitive-data eval where capture must be rejected.
- [ ] Build stale-memory eval where observed repo state supersedes old memory.

Targets:

- High capture precision.
- Low context pollution.
- Low stale-memory injection rate.
- Visible conflict surfacing.

## Refactor / Housekeeping Backlog

These are not release blockers unless they start causing defects.

### H1 — Consolidate Vault Metadata Validation

Five modules share similar closed metadata validation:

- `src/tessera/vault/agent_profiles.py`
- `src/tessera/vault/verification.py`
- `src/tessera/vault/retrospectives.py`
- `src/tessera/vault/compiled.py`
- `src/tessera/vault/automations.py`

Tasks:

- [ ] Add `src/tessera/vault/_validation.py`.
- [ ] Preserve module-specific exception types.
- [ ] Keep validation error messages stable enough for tests.
- [ ] Avoid broad abstractions that hide per-facet contracts.

### H2 — Raw Capture Gate for `compiled_notebook`

Current gates reject raw `capture` for `agent_profile` and `automation`. A symmetric gate for `compiled_notebook` may be warranted.

Tasks:

- [ ] Decide whether generic `capture(facet_type="compiled_notebook")` should fail.
- [ ] If yes, route all writes through `register_compiled_artifact`.
- [ ] Add MCP, REST, and validation tests.

### H3 — Stored-State Corruption Error Consistency

`vault/automations.py` introduced a distinct corruption error. Other modules still conflate caller-input validation and stored-state corruption.

Tasks:

- [ ] Audit `agent_profiles`, `verification`, `retrospectives`, and `compiled`.
- [ ] Add module-specific corruption errors where stored rows are malformed.
- [ ] Avoid masking corruption with default values.

### H4 — CLI Prompt Policy

The current prompt helper fails loud on empty required input. Decide whether interactive commands should re-prompt or fail.

Tasks:

- [ ] Pick policy: re-prompt for interactive sessions or fail-loud for scriptability.
- [ ] Apply consistently in sync setup and future interactive commands.
- [ ] Add tests for required prompt behavior.

### H5 — Filesystem Sync CLI and Conflict Detection

V0.5-P9b ships S3 CLI. `LocalFilesystemStore` is programmatic.

Tasks:

- [ ] Add `--backend filesystem` to `tessera sync setup` if real operators need Dropbox/iCloud/NFS-style sync.
- [ ] Add `tessera sync conflicts` for filesystem conflict artifacts.
- [ ] Detect common sync-provider conflict filenames.

### H6 — S3 Provider Compatibility

Tasks:

- [ ] Add fallback from HEAD bucket to `LIST ?max-keys=0` if real S3-compatible providers reject HEAD.
- [ ] Surface `RequestTimeTooSkewed` as a clear clock-skew error.
- [ ] Consider a local 5-minute clock-skew warning before signing requests.

### H7 — Sync Store Cleanup

Tasks:

- [ ] Add `tessera sync forget-store`.
- [ ] Clear `_meta` sync config.
- [ ] Clear keyring credentials.
- [ ] Clear watermark for the store identity.
- [ ] Do not delete remote blobs or manifests without a separate explicit command.

### H8 — `compiled_artifacts.is_deleted`

Current tombstone state is represented by JOINing to the paired facet row. Keep that design unless row-merge or sync conflict work proves it awkward.

Tasks:

- [ ] Revisit only if V0.5-P9c requires independent artifact tombstone state.

## Strategic Future Work

### S1 — v1.0 Multi-User

Tasks:

- [ ] Define namespace schema.
- [ ] Add per-user-per-namespace scopes.
- [ ] Preserve single-file SQLite inspectability.
- [ ] Add multi-user threat model.
- [ ] Add demo: two users share preferences, isolate projects, both pass cross-facet recall.

### S2 — Optional GUI

Tasks:

- [ ] Decide whether GUI is needed from real support burden.
- [ ] If yes, target read parity first:
  - browse facets
  - inspect recall bundles
  - manage tokens
  - view audit status
  - review capture suggestions

### S3 — Optional Hosted Sync

Tasks:

- [ ] Build only after BYO sync has real users.
- [ ] Preserve BYO storage as free and first-class.
- [ ] Keep hosted service ciphertext-only.
- [ ] Require dogfood by Tom + 3 external users before public launch.

## Non-Goals To Preserve

- No telemetry.
- No auto-capture by daemon.
- No clipboard, screen, or keylogging capture.
- No hosted-only mode.
- No cloud-PaaS default dependency.
- No model reselling.
- No in-process plugin API.
- No in-process LLM compiler.
- No checklist execution engine.
- No scheduler runtime.
- No outbound calls except user-configured BYO sync targets.
- No "tamper-proof" or "immutable" audit-chain language. The claim is tamper-evidence within ADR 0021's boundary.
- No markdown-file replacement for the encrypted vault.
- No daemon hot-path source-code parsing.

## Invariants

- Vault remains a single SQLite file.
- All-local mode remains supported and tested.
- Audit log is complete and append-only.
- Capability tokens remain scoped by operation and facet type.
- Token budgets are enforced at every retrieval surface.
- Default recall is cross-facet.
- Five v0.1 facet types stay stable: `identity`, `preference`, `workflow`, `project`, `style`.
- v0.3/v0.5 facet types are additive.
- `mode='write_time'` is set by compiled-artifact write paths, not by user toggle.
- Tessera stores; callers compile, execute, schedule, and receive.
- Repo-local markdown, if shipped, is an authoring/review adapter over facets, not a parallel memory store.

## Validation Gates

Run before any release tag:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/tessera tests
uv run pytest -m "not slow" --cov=tessera --cov-branch --cov-report=term-missing --cov-fail-under=80
bash scripts/no_telemetry_grep.sh
bash scripts/facet_vocabulary_grep.sh
bash scripts/assume_identity_grep.sh
bash scripts/audit_chain_single_writer.sh
uv run python scripts/audit_chain_determinism.py
```

Run release-relevant targeted tests:

```bash
uv run pytest tests/security/test_audit_chain.py tests/integration/test_cli_audit_verify.py -v
uv run pytest tests/integration/test_sync_round_trip.py tests/integration/test_sync_s3_round_trip.py -v
uv run pytest tests/integration/test_retrieval_pipeline.py tests/integration/test_recall_honesty.py -v
```

Run release-relevant benchmarks:

```bash
uv run python docs/benchmarks/B-RET-1-swcr-ablation/run.py
uv run python docs/benchmarks/B-RET-2-recall-latency/run.py
uv run python docs/benchmarks/B-RET-3-cross-facet-coherence/run.py
uv run python docs/benchmarks/B-SEC-1-encryption-overhead/run.py
```

## Superseded Files

The old enhancement/planning files have been removed after their remaining actionable content was consolidated here:

- `.docs/animocerebro-exchange-analysis.md`
- `.docs/animocerebro-followup-development-plan.md`
- `.docs/animocerebro-followup-execution-prompt.md`
- `.docs/deliberate-capture-recall-enhancement-plan.md`
- `.docs/development-plan.md`
- `.docs/execution-prompt.md`
- `.docs/handoff.md`

Keep these `.docs` paths:

- `.docs/development-plan`
- `.docs/old-benchmarks/`
- `.docs/old-docs/`
- `.docs/user-demo/`
- any public docs under `docs/`

## Revision History

| Version | Date | Change |
| --- | --- | --- |
| 1 | 2026-05-03 | Consolidated remaining tasks from seven internal planning, enhancement, execution, and handoff documents into one canonical `.docs/development-plan`. |
| 2 | 2026-05-04 | Added post-v0.5 project-context layer work from the markdown-codebase-graph analysis: disk-backed markdown context, source refs, integrity checks, explicit expansion, and agent hook scaffolding. |
