# Changelog

All notable changes to Tessera are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Schema v4 (additive over v3, V0.5-P1 + V0.5-P2, ADRs 0016 / 0017)

- New `volatility TEXT NOT NULL DEFAULT 'persistent' CHECK (volatility IN ('persistent', 'session', 'ephemeral'))` column on `facets`. Existing rows default to `persistent` so v0.4 behaviour is preserved.
- New `ttl_seconds INTEGER` column on `facets` carrying the per-row TTL override; `NULL` defers to the volatility default (24 h for `session`, 60 min for `ephemeral`).
- New partial index `facets_volatility_sweep` on `(volatility, captured_at) WHERE is_deleted = 0 AND volatility IN ('session', 'ephemeral')` driving the auto-compaction sweep.
- `facets.facet_type` CHECK constraint extended (V0.5-P2) to reserve the four remaining v0.5 facet types — `agent_profile`, `verification_checklist`, `retrospective`, `automation` — alongside the existing `compiled_notebook` reservation. Reserving every v0.5 type now means subsequent sub-phases (V0.5-P3 / V0.5-P5) activate writes via Python allowlists alone, with no further table-recreate.
- New nullable FK column `agents.profile_facet_external_id TEXT REFERENCES facets(external_id) DEFERRABLE INITIALLY DEFERRED` (V0.5-P2). Points an authentication principal at its canonical `agent_profile` facet without merging the two concepts. Deferrable so `register_agent_profile` can insert the facet and update the agents row in one transaction without the FK firing on the intermediate state.
- Migration step list `_V3_TO_V4_STEPS` registered in `migration/runner.py`. Forward, idempotent, resume-safe; the V0.5-P2 deltas append to the existing P1 step list so the v3 → v4 upgrade path stays cumulative. The `extend_facets_facet_type_check` step uses the SQLite 12-step table-recreate pattern, preserving every column added in P1.

### Added

- **Memory volatility surface (V0.5-P1, ADR 0016).** `tessera capture` now accepts `--volatility {persistent, session, ephemeral}` and `--ttl-seconds <int>`; the MCP `capture` tool and the REST `POST /api/v1/capture` body gain matching parameters with structured-error rejection on illegal combinations (`persistent` rows cannot carry a TTL; `ephemeral` ceiling is 24 h; `session` ceiling is 7 days). The capture audit row records `volatility` and `ttl_seconds` per the §S4 closed-allowlist contract; no content crosses the boundary.
- **SWCR `freshness(f)` term.** SWCR scores are now multiplied by a closed-form decay function: persistent rows contribute `1.0`, `session` rows decay linearly from capture to TTL, `ephemeral` rows step from `1.0` inside the window to `0.0` past it. Deterministic given fixed `now`; the determinism CI gate is unchanged. `swcr.spec.md §Algorithm` documents the closed form alongside the existing `(α, β, γ, λ)` parameters.
- **Auto-compaction sweep.** New `vault/compaction.py` module plus a daemon idle-time loop running every 5 minutes (`_COMPACTION_SWEEP_SECONDS`) that soft-deletes expired session/ephemeral rows via the existing `facets.soft_delete` path. Each compaction emits a `facet_auto_compacted` audit row carrying `facet_type`, `volatility`, and `age_seconds` — no content. Sweeps that compact at least one row also emit a `volatility_sweep` event into `events.db` with totals.
- **Agent profile facet (V0.5-P2, ADR 0017).** New `vault/agent_profiles.py` module owns the structured-metadata contract `{purpose, inputs, outputs, cadence, skill_refs[], verification_ref}`, the register/get/list primitives, and the `agents.profile_facet_external_id` link mutation with audit. Three new MCP tools (`register_agent_profile`, `get_agent_profile`, `list_agent_profiles`) and matching REST routes (`POST /api/v1/agent_profiles`, `GET /api/v1/agent_profiles[/<external_id>]`) ship behind the new `read:agent_profile` / `write:agent_profile` scopes. `recall(facet_types=...)` defaults now include `agent_profile` so the SWCR cross-facet bundle surfaces an agent's profile alongside its related project / skill / verification facets without an explicit filter.
- **Boundary statement enforced.** `agents` remains the JWT subject store; `agent_profile` is the recallable facet. The two are linked via the new nullable FK, never collapsed. `get_agent_profile` adds a per-agent ID guard so a token scoped to one agent cannot read another agent's profile by guessing its ULID. Two new audit ops (`agent_profile_link_set`, `agent_profile_link_cleared`) track every pointer mutation; payloads carry IDs only — profile content and structured metadata never enter audit rows (§S4).
- **Verification + retrospective facets (V0.5-P3, ADR 0018).** Two new vault modules (`vault/verification.py`, `vault/retrospectives.py`) own the closed metadata contracts: checklists carry `{agent_ref, trigger, checks[{id, statement, severity}], pass_criteria}` with severities fixed at `blocker | warning | informational`; retrospectives carry `{agent_ref, task_id, went_well[], gaps[], changes[{target, change}], outcome}` with outcomes fixed at `success | partial | failure`. Three new MCP tools (`register_checklist`, `record_retrospective`, `list_checks_for_agent`) ship behind `read:verification_checklist` / `write:verification_checklist` / `write:retrospective` scopes. REST routes: `POST /api/v1/checklists`, `POST /api/v1/retrospectives`, `GET /api/v1/agent_profiles/<external_id>/checklist`. Cross-agent `agent_ref` writes are blocked at the MCP boundary by an explicit profile-ownership guard so a write-scoped caller cannot plant an artifact pointing at another agent's profile.
- **SWCR retrospective integration.** When the post-rerank working set includes an `agent_profile` facet, the retrieval pipeline augments the candidate set with the most recent N retrospectives whose `agent_ref` matches that profile (default `retrieval.swcr.retrospective_window=3`, configurable to 0 for ablations). Augmented rows enter the SWCR graph at the parent profile's score so the existing cross-type bonus weights the `agent_profile ↔ retrospective` edge naturally. Closed-form, deterministic given fixed `now`; the determinism CI gate is unchanged.
- **Boundary preserved.** Verification is a run-gate, not a guarantee: Tessera stores the checklist; the agent or its caller-side runner executes it. ADR 0018 §Boundary statement mirrors ADR 0020's stance on automations — Tessera registers, callers execute. The `pass_criteria` field is documentation, not enforcement; no checklist execution engine ships in v0.5.
- **Audit-log tamper-evidence (V0.5-P8, ADR 0021).** Two new columns on `audit_log` (`prev_hash` / `row_hash`) form a forward-only linear hash chain over `sha256(prev_hash || canonical_json(event))`. New `vault/canonical_json.py` is the project-local canonicalizer (sorted keys, no whitespace, datetimes as `YYYY-MM-DDTHH:MM:SS.uuuuuuZ`, integers as decimal, floats as shortest round-trip via `repr`, non-ASCII codepoints as `\uXXXX` escapes including surrogate pairs for non-BMP). New `vault/audit_chain.py` owns the canonical insert path (`audit_log_append`) and the chain walker (`verify_chain`); `vault.audit.write` delegates to it so every callsite from capture / retrieval / auth / daemon / migration goes through one chain-aware function. Migration step backfills pre-upgrade rows in id-ASC order — pre-upgrade tampering is not retroactively detectable per ADR 0021 §Security claim — exact boundary.
- **`tessera audit verify` CLI.** New `audit_cmd` subcommand walks the chain end-to-end. Exit codes per ADR 0021 §Verify CLI: `0` = chain intact (reports total rows + genesis id + head id), `1` = first broken row (reports row id, op, expected vs stored `row_hash`), `2` = schema/vault error. Help text quotes the claim boundary verbatim so users see exactly what the chain detects and what it does not before treating exit 0 as a stronger guarantee than it is.
- **Three new CI gates.** `audit-chain-determinism` runs the canonicalizer against a fixed input vector twice and asserts byte-identical output; `audit-chain-single-writer` rejects any direct `INSERT INTO audit_log` outside `vault/audit_chain.py` (the canonical insert path); `audit-chain-verify-on-test-vault` runs the seven security tests (genesis, append, deletion-detect, modify-detect, reorder-detect, insert-detect, full-walk-clean) against a populated vault under `TESSERA_NO_OUTBOUND=1`. All three are blocking on every PR.
- **Public language stays honest.** Release notes, threat-model §S4, and the CLI help text refer to the chain as **tamper-evidence within the stated claim boundary**, never **tamper-proof** or **immutable**. The chain detects accidental corruption + non-recomputed tampering; detecting tampering by a recompute-capable attacker requires keyed/signed/anchored variants which are deferred to v1.0 per ADR 0021 §Public language guardrails.
- **Compiled notebook = AgenticOS Playbook (V0.5-P4, ADR 0019).** Activates `compiled_notebook` for writes and lands the `compiled_artifacts` table per the v2 reservation. New `vault/compiled.py` owns the pair-write transaction (one external_id maps to a `compiled_notebook` facet AND a `compiled_artifacts` row) so SWCR cross-facet bundles surface the playbook alongside its sources via the standard recall path. Three new MCP tools (`register_compiled_artifact`, `get_compiled_artifact`, `list_compile_sources`) and matching REST routes (`POST /api/v1/compiled_artifacts`, `GET /api/v1/compiled_artifacts/<external_id>`, `GET /api/v1/compile_sources?target=...`) ship behind the new `read:compiled_notebook` / `write:compiled_notebook` scopes. `compiled_notebook` enters the dispatcher's `_DEFAULT_RECALL_TYPES` so a bare `recall` answers "what does my AgenticOS look like?" without an explicit filter.
- **Out-of-process compiler boundary.** Per ADR 0019 §Boundary statement Tessera stores compiled artifacts; the caller compiles them. The two-call API (`list_compile_sources` / `recall` to read, `register_compiled_artifact` to write) lets any caller pick its own compiler. There is no `compile_now()` API, no in-process LLM, no auto-compile. The `pass_criteria` for what counts as a good playbook is the caller's responsibility; Tessera enforces only the schema invariants. Source facets carry `metadata.compile_into = ['target']` for the source-tag pattern; `list_for_compilation` enumerates the four ADR-0019 source types (`agent_profile`, `project`, `skill`, `verification_checklist`) and filters by the tag.
- **New audit op `compiled_artifact_registered`.** Allowlist payload: `{artifact_type, compiler_version, source_count}`. Source ULIDs stay on `compiled_artifacts.source_facets` (the JSON array column) rather than the audit row, so §S4 stays inside the no-user-content contract. The pair-write rides through the V0.5-P8 chain insert path; `tessera audit verify` succeeds on a vault that has ingested compiled artifacts (covered by the new `test_chain_full_walk_clean_with_compiled_artifacts` security test).
- **V0.5-P8 ship-gate satisfied.** V0.5-P4 only ships because V0.5-P8 (audit chain) is green on `main` — the audit chain ship-gate per ADR 0019 §Rationale (8) and ADR 0021 §Public language guardrails. The cross-test `test_chain_full_walk_clean_with_compiled_artifacts` is the load-bearing assertion that the two surfaces compose correctly: the chain walks cleanly across `facet_inserted` + `compiled_artifact_registered` rows even when the chain payload includes the synthesized state V0.5-P4 introduces.
- **Compiled-artifact staleness wiring (V0.5-P6, ADR 0019 §Rationale 6).** Activates the `is_stale` flag on `compiled_artifacts` that V0.5-P4 committed but did not flip. New `vault/compiled.py:mark_stale_for_source` walks `compiled_artifacts.source_facets` (JSON array column) via SQLite's `json_each` and flips every artifact citing the mutating source's external_id from `is_stale=0` to `is_stale=1`. Hooked into the three source-mutation paths the handoff names: `vault.capture.capture` (after the audit row), `vault.facets.soft_delete` (which now looks up the row's `agent_id` so the side-effect can scope its lookup), and `vault.skills.update_procedure`. `update_metadata` does **not** trigger — only procedure-body changes invalidate the compiled narrative. Direct membership only — ADR 0019 §Rationale (6) and the V0.5 handoff Open Question (4) reject transitive propagation; an `agent_profile` whose metadata cites a skill ULID does not cascade when the skill mutates unless the artifact's `source_facets` list cites the skill directly.
- **Tombstone filter on `compiled.get` / `list_for_agent` (PR #61 review M1).** Both helpers now JOIN `compiled_artifacts` against `facets` on `external_id` and filter `WHERE facets.is_deleted = 0`. A `forget` against a `compiled_notebook` facet automatically tombstones the artifact via the JOIN — single source of truth for tombstone state stays on the facet row. No schema delta; no parallel `compiled_artifacts.is_deleted` column. Recommendation from the V0.5 handoff "Other pending follow-ups" §3.
- **New audit op `compiled_artifact_marked_stale`.** Allowlist payload: `{source_external_id, source_op}`. Emitted once per artifact that flips. `source_external_id` is the ULID of the mutating source facet; `source_op` records which mutation path emitted the flip (`facet_inserted` / `facet_soft_deleted` / `skill_procedure_updated`) so forensics can reconstruct the cascade in one query. Source content, query text, and metadata never enter the payload — §S4 boundary preserved. The cascade rows ride the V0.5-P8 chain insert path; `test_chain_full_walk_clean_with_compiled_staleness` proves the chain walks cleanly across stale events alongside `facet_inserted` + `compiled_artifact_registered`.
- **Idempotency + cross-agent isolation invariants.** `mark_stale_for_source` filters at the WHERE clause for `is_stale = 0` so a second mutation against an already-stale artifact emits no second audit row. The lookup is scoped by `agent_id` so a leaked ULID surfaced in another agent's source list cannot trigger a cross-agent stale flip — `test_cross_agent_mutation_does_not_cascade` plants the impossible-via-public-write shape directly to prove the staleness primitive's `agent_id` filter is independently load-bearing.
- **Capture hook gates on un-delete only (PR #62 review H1).** `facets.insert` has three branches under content-hash dedup: brand-new (fresh ULID), un-delete (soft-deleted match restored), and live-duplicate (already-live match, no SQL mutation). The capture-side staleness hook reads the prior `is_deleted` snapshot before insert and fires only when the un-delete branch landed. Brand-new captures skip cleanly (no possible cite); live-duplicate captures skip because no source state changed (firing would invert "no change → no flip"). Regression test `test_recapture_live_duplicate_does_not_flip`.
- **Automation registry, storage-only (V0.5-P5, ADR 0020).** Activates `automation` for writes — the final v0.5 reserved facet type, closing the v0.5 vocabulary. New `vault/automations.py` module owns the closed metadata contract `{agent_ref, trigger_spec, cadence, runner}` plus optional `{last_run, last_result}` and the `register` / `record_run` / `get` / `list_for_agent` primitives. Two new MCP tools (`register_automation`, `record_automation_run`) and matching REST routes (`POST /api/v1/automations`, `POST /api/v1/automations/<external_id>/runs`) ship behind `read:automation` / `write:automation` scopes. List/get read paths reuse `recall`, `list_facets`, and `show` per ADR 0020 §Rationale 3 — no separate read tools. The `runner` field on `list_for_agent` filters the registry to "my automations" without scanning the whole list.
- **Storage-only boundary enforced.** Per ADR 0020 §Boundary statement Tessera registers automations as data; runners (Claude Code `/schedule`, OpenClaw HEARTBEAT, cron, systemd timers, GitHub Actions, custom shell loops) execute them. There is no scheduler runtime, no outbound trigger, no in-process timer. The daemon does not learn about an automation until a caller writes it; it does not act on the existence of an automation; it does not fire when one is "due." `next_run` is intentionally omitted per ADR 0020 §Rationale 5 — a computed next-run would imply Tessera knows when to fire.
- **Bypass-via-raw-capture gate.** The MCP `capture` tool rejects `facet_type='automation'` (mirror of the V0.5-P2 `agent_profile` gate) so the structured-metadata contract cannot be bypassed by writing through the generic capture path. Routing every automation write through `register_automation` upholds the "every stored row is parseable" invariant the storage-only registry depends on.
- **New audit op `automation_run_recorded`.** Allowlist payload: `{result_bucket, last_run_at}`. `result_bucket` is the canonical bucket (`success` / `partial` / `failure`) or `"other"` for free-form notes — caller prose stays out of the audit chain per the §S4 boundary; the runner's full `last_result` lives on the row's metadata column. `last_run_at` is an ISO-8601 timestamp the runner supplied. Unlike `compiled_artifact_marked_stale`, this op is **not** idempotent — every `record_run` is a genuine state transition with a fresh timestamp, so the chain grows by one row per call. Ship-gate companion `test_chain_full_walk_clean_with_automation_runs` walks a vault that has accumulated multiple `automation_run_recorded` rows and asserts the chain stays clean.
- **Cross-agent isolation.** `record_run` filters by `agent_id` at the SQL layer (raises `UnknownAutomationError` rather than silently no-op'ing a cross-agent update). The MCP layer's `_enforce_same_agent_profile_ref` guards the `agent_ref` field on `register_automation` so a write-scoped caller cannot plant an automation that points at another agent's profile. Tests `test_record_automation_run_blocks_cross_agent_update` and `test_register_automation_blocks_cross_agent_ref` pin both boundaries.
- **Distinct corruption-class error (PR #63 review fold).** `vault.automations.CorruptAutomationRowError` separates "the stored row is malformed" from "the caller sent bad input" (`InvalidAutomationMetadataError`). The MCP layer maps the former to `StorageError` and the latter to `ValidationError` so operators can distinguish vault corruption from caller bugs in logs and forensics. Surfaces in both `record_run` (existing-row JSON-decode + post-merge revalidation) and `_row_to_automation` (read-path JSON-decode + contract drift). Regression tests `test_record_run_surfaces_corrupt_metadata_distinctly` and `test_get_surfaces_corrupt_metadata_distinctly` plant a malformed metadata blob and assert the correct error class.
- **`record_run` UPDATE WHERE clause defense-in-depth (PR #63 review fold).** The UPDATE predicates now mirror the SELECT (`external_id` AND `facet_type='automation'` AND `agent_id=?` AND `is_deleted=0`). Currently safe under SELECT alone (`external_id` is UNIQUE), but a future schema change relaxing uniqueness could otherwise let the UPDATE silently mutate the wrong row. Symmetric predicates make the invariant local to the function rather than load-bearing on schema-level UNIQUE.
- **Read-path coverage and tombstone regression guard (PR #63 review fold).** New integration tests prove `list_facets`, `show`, and `forget` all work for automation rows — closing the "stored but unrecallable" silent-failure mode ADR 0020 §Rationale 3 leans against by reusing the generic read surface. New unit test `test_record_run_blocks_soft_deleted_automation` pins the SQL filter so a refactor that drops `is_deleted = 0` cannot let runners mutate tombstoned rows.
- **Default-recall enrollment pinned.** `dispatch._DEFAULT_RECALL_TYPES` is dynamically derived from `WRITABLE_FACET_TYPES`, so V0.5-P5 auto-enrolls `automation` in the cross-facet recall default. New regression test `test_default_recall_types_includes_automation` pins the property explicitly so a future refactor switching to a static list cannot silently drop the type.
- **Bucket allowlist + length-bound parametrisation.** `test_record_run_buckets_each_canonical_value` covers all three canonical buckets (`success`, `partial`, `failure`) plus two free-form values bucketed to `"other"`. `test_validate_metadata_rejects_overlong_required_field` parametrises the length-cap rejection across every required string field; new tests pin the optional-field caps. A regression mistyping a bucket or loosening a cap surfaces as a missing parametric case.
- **Recall surfaces compiled-artifact mode + staleness (V0.5-P7, ADR 0019 §Retrieval surface).** Every `recall` match now carries `mode` (the row's production method) and `is_stale` (the V0.5-P6 staleness flag for `compiled_notebook` rows). For non-compiled facet types the fields default to `mode='query_time'` and `is_stale=False` so callers do not need facet-type-specific branches. The retrieval pipeline's `_to_matches` hydrates the K survivors via a single `LEFT JOIN compiled_artifacts ON external_id` SQL pass — cost is proportional to response size, not to the ~50 BM25/dense candidates the earlier stages consider. The `RecallMatchView` MCP dataclass and the dispatch JSON shape both gain the two fields; the retrieval-pipeline `RecallMatch` is the load-bearing carrier between layers.
- **No privileged slice for compiled artifacts.** Per ADR 0019 §Retrieval surface the bundle's token budget envelope treats compiled artifacts as one more facet type competing with the others — SWCR coherence weights apply uniformly. The `mode`/`is_stale` fields are pure metadata; they do not influence ranking, scoring, or budget allocation. Test `test_recall_returns_query_time_mode_for_non_compiled_matches` pins the uniform-shape contract; integration `test_recall_match_view_carries_mode_and_is_stale` proves the round-trip through the MCP boundary.
- **End-to-end staleness propagation.** `test_recall_match_view_surfaces_is_stale_after_mutation` registers a compiled artifact, calls `forget` on a source, then asserts the next `recall` surfaces `is_stale=True` on the artifact's match. Together with V0.5-P6 the chain is: source mutation → `mark_stale_for_source` flips the flag → `_hydrate_match_metadata` reads the updated flag → recall surfaces it without an explicit re-fetch.

### ADRs

- ADR-0016 — memory volatility model. Documents the planned `volatility` column on `facets` (`persistent` | `session` | `ephemeral`), TTL + auto-compaction policy, and SWCR freshness weighting for non-persistent rows. Documents-only landing as part of the v0.5 ADR sequence; the schema delta is implemented in a later v0.5 sub-phase.
- ADR-0017 — agent profile as a first-class facet. Documents the planned `facet_type='agent_profile'`, its structured metadata shape, and the nullable FK linkage from `agents.profile_facet_external_id` to a profile facet. Records the explicit boundary that the `agents` table remains the JWT subject store while the profile facet is the recallable context — the two are linked, not merged. Documents-only as part of the v0.5 ADR sequence.
- ADR-0018 — verification + retrospective facets. Documents the planned `facet_type='verification_checklist'` and `facet_type='retrospective'`, their structured metadata shapes, three new MCP tools, and the SWCR retrospective integration that surfaces a configurable window of recent retrospectives whenever an `agent_profile` enters the candidate set. Records the boundary statement that Tessera stores checklists, callers execute them. Documents-only as part of the v0.5 ADR sequence.
- ADR-0019 — compiled notebook as the AgenticOS Playbook. Documents the unified shape for `facet_type='compiled_notebook'`: one type covering both vertical-depth research synthesis and the AgenticOS Playbook framing, one compiler boundary (out-of-process, two-call API), one storage path (existing `compiled_artifacts` table), and one staleness signal. Records the ship-gate that V0.5-P4 (write-time compilation) must not merge before V0.5-P8 (audit chain) is green. Documents-only as part of the v0.5 ADR sequence.
- ADR-0020 — automation registry, storage-only. Documents the planned `facet_type='automation'`, its metadata shape (`agent_ref`, `trigger_spec`, `cadence`, `runner`, `last_run`, `last_result`), two new MCP tools, and the integration pattern for caller-side runners (Claude Code `/schedule`, OpenClaw HEARTBEAT, cron, etc.) reading the registry via REST + `tessera curl` recipes. Records the explicit boundary that Tessera registers automations; runners execute them — no scheduler runtime, no outbound triggers, no in-process timer. Documents-only as part of the v0.5 ADR sequence.
- ADR-0021 — audit-chain tamper evidence. Documents the planned `prev_hash` + `row_hash` columns on `audit_log`, the single `audit_log_append` insert path, the project-local `canonical_json` determinism contract, the `tessera audit verify` CLI exit codes, and the seven security tests. Records the exact security claim boundary: a plain local linear hash chain detects accidental corruption + non-recomputed tampering; detecting tampering by a recompute-capable attacker requires keyed/signed/anchored variants which are deferred to v1.0. Locks V0.5-P8 as the hard ship-gate before V0.5-P4 reaches users. Supersedes the AnimoCerebro plan §Phase D placeholder. Documents-only as part of the v0.5 ADR sequence.

## [0.4.0rc2] — 2026-04-27 (pre-release)

First-run ergonomics fix on the v0.4 line. rc1 shipped earlier the same day with a 30 s `tessera daemon start --timeout` default that proved too tight after the ONNX-only migration: the daemon's first start now downloads ~650 MB of fastembed weights on the critical startup path, which routinely takes 30–90 s on a typical residential link. When the CLI hit the timeout, the spawned `tesserad` kept running in the background and eventually bound port 5710 — but the user's next `tessera daemon start` then hit `OSError [Errno 48] address already in use` against the orphan it had just abandoned. rc2 raises the default so the timeout no longer races the download.

### Changed

- `tessera daemon start --timeout` default bumped from 30 s to 120 s. Subsequent starts (cache warm) complete in ~3–5 s and stay well under the new default. Users on faster paths can pass `--timeout 30` for the previous behaviour; users on slower links pass `--timeout 600` once. The argparse help text picks the value up automatically.

### Install

```bash
pip install --pre tessera-context
# or pin explicitly:
pip install tessera-context==0.4.0rc2
```

No vault migration required from rc1; this rc is a CLI-default change only.

## [0.4.0rc1] — 2026-04-27 (pre-release)

Tessera v0.4 swaps the entire model stack to **fastembed (ONNX Runtime) running fully in-process** and removes Ollama, sentence-transformers, OpenAI, and Cohere adapters from the codebase. The torch dependency closure goes with them. Install footprint drops from ~600 MB to ~30 MB of Python packages. The change is breaking: any vault embedded by v0.1–v0.3 needs a re-embed against fastembed weights (run `tessera models set --name <fastembed-id> --activate` then `tessera vault repair-embeds`, or wipe and re-init for a clean start).

The same release introduces a **REST surface at `/api/v1/*`** alongside the existing `/mcp` endpoint and a `tessera curl` recipe builder for hooks, skills, and shell scripts. Both transports share one daemon dispatcher; the REST envelope is leaner (raw result dict, no JSON-RPC wrapper) so hook authors save 50–150 tokens per call.

CLI ergonomics also tightened: `--vault` and `--passphrase` are optional on every subcommand with `flag → $TESSERA_VAULT/$TESSERA_PASSPHRASE → default` resolution; `tessera init` creates `~/.tessera/vault.db` by default. ADR-0014 records the embedder swap; ADR-0013 records the REST surface decision.

### Removed

- **Ollama embedder, sentence-transformers reranker, Cohere reranker, OpenAI embedder, and the torch-based device-detection helper** — `src/tessera/adapters/{ollama_embedder,st_reranker,cohere_reranker,openai_embedder,devices}.py` deleted. The `ollama_host` field on `DaemonConfig` and the `OLLAMA_HOST` / `TESSERA_OLLAMA_MODEL` environment variables are gone. The doctor's `_check_ollama` is replaced with a fastembed cache check.
- `ollama` and `sentence-transformers` dropped from `dependencies`. `torch`, `transformers`, `tokenizers`, `safetensors`, `scipy`, `sympy`, `scikit-learn`, and the rest of the torch closure are gone transitively.

### Added

- **fastembed embedder + reranker** as the sole adapter for both roles. `src/tessera/adapters/fastembed_embedder.py` defaults to `nomic-ai/nomic-embed-text-v1.5` (768 dim); `src/tessera/adapters/fastembed_reranker.py` defaults to `Xenova/ms-marco-MiniLM-L-12-v2` (cross-encoder ONNX export). Both run in-process via ONNX Runtime; no separate model server, no torch.
- ADR-0014 (ONNX-only model stack via fastembed) records the switch and supersedes ADR-0006. ADR-0008 partially superseded.
- REST surface at `/api/v1/*` alongside `/mcp`, sharing the daemon dispatcher, capability-token auth, and scope checks. Endpoints: `POST /api/v1/capture`, `GET /api/v1/recall`, `GET /api/v1/stats`, `GET /api/v1/facets[/<external_id>]`, `DELETE /api/v1/facets/<external_id>`, `POST /api/v1/skills`, `GET /api/v1/skills[/<name>]`, `GET /api/v1/people`, `GET /api/v1/people/resolve`. Lean error envelope: `{"error": {"code", "message"}}` with HTTP 4xx/5xx, no top-level `ok` flag.
- `tessera curl <verb>` subcommand that prints copy-pasteable curl recipes for each REST endpoint, or executes them and pipes the JSON response. `--print` mode keeps `${TESSERA_TOKEN}` unexpanded so recipes are safe to commit to hook scripts.
- `docs/api.md` — canonical REST reference with per-endpoint URL/verb/params/response and worked recipes for pre-prompt hooks, post-tool capture hooks, and daily backup scripts.
- ADR-0013 — REST surface alongside MCP. Records the dual-transport decision and scopes its boundary with ADR-0005.
- `--vault` and `--passphrase` are now optional on every CLI subcommand. Resolution order: explicit flag → env var (`TESSERA_VAULT` / `TESSERA_PASSPHRASE`) → default (`~/.tessera/vault.db`). Multi-vault disambiguation when `~/.tessera/` contains multiple `*.db` files and no choice was made.
- `docs/quickstart.md §Setup once` — env-var setup for flag-free daily use.
- `docs/troubleshooting.md` sections on persistent passphrase setup, multi-vault disambiguation, and the `NoActiveModelError` symptom.
- `docs/smoke-test-v0.4rc1.md` — clean-install runbook for v0.4 (replaces the v0.3 runbook).

### Changed

- `embedding_models.name` column now stores the fastembed model identifier directly (e.g. `"nomic-ai/nomic-embed-text-v1.5"`) instead of an adapter slot label. The previous indirection — adapter slot in `name`, provider model behind `TESSERA_OLLAMA_MODEL` — collapsed to a single column once fastembed became the sole adapter. The registry's "must be a known adapter" pre-check is removed (fastembed validates at first embed call).
- `tessera init` no longer requires `--vault`; it creates `~/.tessera/vault.db` (or `$TESSERA_VAULT`) by default and creates the parent directory if missing.
- The "passphrase required" error points users at the persistent `export TESSERA_PASSPHRASE` path instead of the per-call `--passphrase` flag.
- `tessera doctor` replaces the Ollama HTTP probe with a fastembed import + cache-directory check.
- `tessera models set` defaults updated; `tessera models test` now instantiates a `FastEmbedEmbedder` and calls `health_check`.

### Install

```bash
pip install --pre tessera-context
# or pin explicitly:
pip install tessera-context==0.4.0rc1
```

The first daemon start after `tessera models set --activate` downloads the embedder weights (~520 MB for `nomic-ai/nomic-embed-text-v1.5`, ~130 MB for the `-Q` quantised variant) plus the reranker weights (~130 MB) to `~/.cache/fastembed`. One-time cost; offline thereafter.

## [0.3.0rc1] — 2026-04-26 (pre-release)

Tessera v0.3 activates the **People + Skills surface** and ships the first **conversation-history importers** (ChatGPT and Claude). Schema bumps to v3 with an additive, idempotent v2 → v3 migration. Design rationale is recorded in [ADR-0012](docs/adr/0012-v0-3-people-and-skills-design.md). v0.3 DoD lives at [`docs/release-spec.md §Definition of Done for v0.3`](docs/release-spec.md). Release-engineering decision folding v0.1 DoD items 1 and 9 into the v0.3.0rc1 gate is recorded at [`docs/v0.1-dod-audit.md §Decision 2026-04-26`](docs/v0.1-dod-audit.md). Cross-platform clean-VM walkthrough runbook: [`docs/smoke-test-v0.3rc1.md`](docs/smoke-test-v0.3rc1.md). Friend-share onboarding: [`docs/quickstart.md`](docs/quickstart.md).

### Schema v3 (additive over v2)

- New nullable `disk_path` column on `facets`, partial-unique-indexed per agent so each on-disk skill file maps to at most one live row.
- New `people` table — separate from `facets` — with `canonical_name`, JSON `aliases` array, and `UNIQUE(agent_id, canonical_name)`.
- New `person_mentions(facet_id, person_id, confidence)` link table with `ON DELETE CASCADE` on both foreign keys.
- v2 → v3 step list registered in `migration/runner.py::_V2_TO_V3_STEPS` (idempotent, resume-safe, takes a pre-migration backup).

People are stored as rows, not facets, because relationship-graph mutability (alias merges, splits) fights `UNIQUE(agent_id, content_hash)` dedup. Skills are facets with structured metadata (`{name, description, active}`) plus the optional `disk_path` column. ADR-0012 §Rationale records the alternatives and rejection reasons.

### New facet type activated for writes

- `skill` — user-authored procedure markdown, optionally synced to disk. The `content` field carries the procedure verbatim; `disk_path` links it to a `.md` file.

### Five new MCP tools

- `learn_skill(name, description, procedure_md)` — write scope on `skill`.
- `get_skill(name)` — read scope on `skill`, returns `null` when no live match.
- `list_skills(active_only=true, limit=50)` — read scope on `skill`.
- `resolve_person(mention)` — read scope on `person`, returns `(matches, is_exact)`. Conservative: a single canonical-name or alias match flips `is_exact=True`; multi-match or substring hits return every candidate. Auto-pick is deliberately not wired (no calibration data at v0.3; a wrong auto-pick is hard to undo).
- `list_people(limit=50, since?)` — read scope on `person`.

### New CLI

- `tessera skills {list, show, sync-to-disk, sync-from-disk}` — list/show via HTTP MCP; sync via direct vault access.
- `tessera people {list, show, merge, split}` — list/show via HTTP MCP; merge/split via direct vault access.
- `tessera import {chatgpt, claude} <path>` — direct-vault batch import.

The shared HTTP-MCP helpers (`tessera capture`, `tessera skills list`, `tessera people show`, …) were extracted from `cli/tools_cmd.py` into a new `cli/_http.py` module so the `httpx` import lives in exactly one place. The CI no-telemetry allowlist tracks the move.

### Importers

- ChatGPT (`conversations.json` from a ChatGPT data export) — walks the active-branch via the export's mapping graph; falls back to a `create_time` sort when `current_node` is missing or the parent chain is broken; handles multimodal `content` block arrays.
- Claude (`conversations.json` from a Claude data export) — walks the flat `chat_messages` array; handles both legacy `text` and newer `content` block shapes.

Both importers write **only `project` facets** by ADR-0012's design — never `skill` or `person`. Skills stay user-authored via `learn_skill`; people surface through `resolve_person`. Person-mention auto-extraction during import is documented future work; shipping heuristic NER without calibration data would create silent false-positive person rows the user can't easily undo.

### Default recall fan-out

`recall` without an explicit `facet_types` filter now includes `skill` in the cross-facet bundle by default. `person` is excluded — people live in their own table, have no embeddings, and are served by the dedicated `resolve_person` tool.

### Documentation

- ADR-0012 — v0.3 People + Skills design.
- v0.3 DoD checkboxes added to `docs/release-spec.md` covering cross-platform smoke (subsumes v0.1 DoD item 1), v2 → v3 migration verification on a real rc2 vault, and carry-over of v0.1 DoD item 9 (external user demo) as the rc1 → GA gate.
- `docs/smoke-test-v0.3rc1.md` runbook with VM baselines, Flow A (clean install), Flow B (rc2 → rc1 in-place migration), failure-mode table, and gate-closure criteria.
- `docs/quickstart.md` friends-share onboarding guide.

### Known limitations (v0.3)

- **Person-mention auto-extraction during import is not shipped.** Documented future work pending calibration data.
- **Skill names must be unique per agent.** A user who names two skills the same hits `DuplicateSkillNameError` on the second `learn_skill` call. No `learn_skill_or_overwrite` variant in v0.3.
- **People accumulate without garbage collection.** A user importing a ChatGPT export with many one-off person mentions has only `tessera people merge` for consolidation. Re-evaluate at v0.5 if real-user vaults grow unwieldy.
- **No write-time compilation, no episodic temporal retrieval, no BYO sync.** Deferred to v0.5.
- **HMAC-chained audit log** remains v0.3 scope per the v0.1 threat model — implementation lands later in the v0.3.x window.

### Install

```bash
pip install --pre tessera-context
# or pin explicitly
pip install tessera-context==0.3.0rc1
```

The v0.3.0rc1 → v0.3.0 GA stabilization gates (none of which block rc1 publication) are: cross-platform clean-install smoke recordings on macOS / Ubuntu / Windows per `docs/smoke-test-v0.3rc1.md`, the v2 → v3 migration verified on a real rc2 vault on each platform, one external user completing the T-shape demo unaided (carry-over of v0.1 DoD item 9), and 30+ days of Tom dogfooding ChatGPT/Claude imports on a real vault. rc1 ships now on internal evidence (CI green, schema v3 migration covered by unit tests, the v0.3 surface covered by integration tests) — same pattern as v0.1.0rc1 and rc2.

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
