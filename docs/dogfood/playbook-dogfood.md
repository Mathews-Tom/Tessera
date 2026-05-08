# Playbook Dogfood Evidence

**Status:** Pending external evidence.

This document tracks the Phase 9 dogfood gate from `.docs/compiled-playbooks-enhancement-plan.md` for task-shaped Playbooks. It does not close the gate until at least one named operator has compiled, recompiled, and used real Playbooks against a working Tessera vault long enough to surface representative failure cases. The companion `docs/dogfood/compiled-notebook-dogfood.md` gates the v0.5 write-time-compilation DoD bullet at the artifact level; this document gates the task-shaped Playbook surface (compile target descriptors, eval-set conventions, field provenance, the `tessera playbook` CLI, and the recipe pack) shipped in PRs #73–#82.

## Gate

The gate is complete only when all of the following are true:

- At least two of the four Phase 9 dogfood targets are registered as real Playbooks against a working Tessera vault.
- One named operator drives every compile, recompile, and source mutation through the shipped CLI surface (`tessera playbook scaffold`, `tessera playbook register`, `tessera playbook stale`, and `tessera playbook inspect`); no compiled artifact is registered through hand-written SQL or the Python API for the run.
- Each registered artifact carries the seven minimum sections from `docs/playbook-compiler-recipes.md §Minimum artifact sections`, the recipe-pack `compiler-version` string, and a non-empty `## Eval summary` block.
- At least one source facet referenced by a registered artifact mutates during the run; `tessera playbook stale --json` lists the affected `external_id` and the cascade cause is recorded in the evidence log.
- The stale artifact is recompiled through the same recipe; the new artifact carries the same `metadata.target` value, ships with `is_stale=false`, and becomes the most-recent-fresh candidate for `[[playbook:<target>]]` and target-keyed lookups. The previous artifact stays in storage with `is_stale=true` and remains inspectable through `tessera playbook inspect <ulid>` until it is explicitly forgotten or compacted.
- `tessera audit verify` returns exit 0 on the vault before the run, after the first `register`, after the staleness flip, and after the recompile.
- Failure cases are captured verbatim under the `## Failure cases` log, even when the run otherwise succeeds. An empty failure log is a recipe smell, not a clean run.
- The ranking-penalty decision (Phase 4 open question) is resolved with a recorded recommendation: keep "no penalty, loud metadata" or land the closed-form ranking adjustment described in `.docs/compiled-playbooks-enhancement-plan.md §Phase 4`.
- No unresolved data-loss, audit-chain, provenance, or compiled-artifact integrity bug remains open at the end of the run.

Synthetic unit tests, throwaway demo artifacts, and the recipe-pack examples do not satisfy this gate. Implementation behavior is covered by the compiled-artifact, staleness, recall-surface, and audit-chain test suites; this document tracks the longitudinal product evidence those tests cannot supply.

## Dogfood targets

The four Phase 9 targets, each tied to one recurring task. The gate clears at two registered targets; the remaining two stay queued for follow-on dogfood.

| Target | Recurring task | Success criterion | Recipe |
| --- | --- | --- | --- |
| `tessera_release_playbook` | execute release prep consistently | reduces release-context gathering and catches missing gates before a `vX.Y.0rcN` cut | `claude-code/release-recipe@YYYY-MM-DD` |
| `swcr_design_brief` | answer SWCR architecture and retrieval-design questions | answers representative design questions with source-backed claims and no invented ULIDs | `claude-code/swcr-recipe@YYYY-MM-DD` |
| `dissertation_memory_chapter` | maintain vertical-depth research context | remains useful for the operator's research work across 30+ active days | `manual/research-recipe@<semver-or-date>` |
| `project_context_adapter_brief` | guide v0.6 project-context-layer design | clarifies source refs, integrity checks, and `tessera expand` decisions for the v0.6 scope | `claude-code/context-recipe@YYYY-MM-DD` |

Recipe identifiers follow the runner-name / recipe-name / version convention from `docs/playbook-compiler-recipes.md §Compiler version naming`. The dates above are placeholders; the operator stamps the active date on each compile.

## Recording protocol

Evidence accrues in the JSONL ledger at `~/.tessera/dogfood/playbook.jsonl` (override with `$TESSERA_DOGFOOD_DIR`). Every row carries a real `machine_id`, real timestamp, and the Tessera version that emitted it. Synthetic rows are not allowed; the ledger is append-only and the relevant CLI commands auto-emit one row each per real invocation when the gate is active.

Auto-emitting commands once `tessera dogfood init playbook` has run:

| Command | Auto-emitted kind | Notes |
| --- | --- | --- |
| `tessera playbook register` | `register` | target + external_id + compiler_version + source_count + exit_code + elapsed_ms (also emits to the compiled gate when active) |
| `tessera playbook stale` | `stale_event` | most recent cascade cause + total stale count (only when the listing is non-empty) |
| `tessera audit verify` | `audit_verify` | exit_code + outcome (`intact` / `empty_chain` / `broken_row` / `schema_error`) |

The recompile flow emits one `register` row for the new artifact (auto-hooked) and one explicit `recompile` row that ties the new ULID back to the stale one — the dogfood predicate "Recompile produced fresh artifact preserving `target`" reads that link to clear.

Open the gate before the run starts:

```bash
tessera dogfood init playbook \
  --operator "Tom Mathews" \
  --start-date 2026-05-09 \
  --field targets_in_scope=tessera_release_playbook,swcr_design_brief \
  --field recipe_pack=docs/playbook-compiler-recipes.md \
  --field vault_schema_version=4
```

Compile loop per target (the `register` row auto-emits on the last command):

```bash
tessera playbook scaffold tessera_release_playbook --out brief.md
# ... runner of choice produces playbook.md from brief.md ...
tessera playbook register tessera_release_playbook \
  --content playbook.md \
  --compiler-version claude-code/release-recipe@2026-05-09
```

Stale loop after a source mutation:

```bash
tessera playbook stale            # emits stale_event when the listing is non-empty
tessera audit verify              # emits audit_verify to every active gate
```

Recompile loop (auto-emits `register` for the new artifact; record the recompile link explicitly so the predicate can verify the `target` carryover):

```bash
tessera playbook register tessera_release_playbook \
  --content playbook.recompiled.md \
  --compiler-version claude-code/release-recipe@2026-05-23
tessera dogfood record playbook --kind recompile \
  --field target=tessera_release_playbook \
  --field old_external_id=01H...PRIOR \
  --field new_external_id=01H...FRESH \
  --field compiler_version=claude-code/release-recipe@2026-05-23
```

Failure-case logging is mandatory; every Phase 9 failure class needs at least one entry, even if the entry is a "not observed" note:

```bash
tessera dogfood record playbook --kind failure_case \
  --field failure_class=stale_artifact_trusted_accidentally \
  --field target=tessera_release_playbook \
  --field corrective_action="expanded brief to call out is_stale up front" \
  --field text="agent skimmed past the stale warning"
```

Allowed `failure_class` values: `stale_artifact_trusted_accidentally`, `source_missing_from_compiled_output`, `eval_passed_but_answer_was_weak`, `artifact_too_lossy_for_exploratory_use`, `other`. Unknown values are rejected at the CLI boundary.

Record the ranking-penalty decision once enough cases have accrued:

```bash
tessera dogfood record playbook --kind decision \
  --field decision_id=ranking_penalty \
  --field recommendation="no penalty, loud metadata" \
  --field text="3 observed cases where loud metadata kept the agent from trusting stale artifacts"
```

Re-render the published tables:

```bash
tessera dogfood render playbook            # rewrites this doc between markers
tessera dogfood render playbook --no-write   # prints without writing
```

Close the gate when the run ends:

```bash
tessera dogfood record playbook --kind gate_completed \
  --field end_date=2026-07-08 \
  --field outcome=clean
```

Set `TESSERA_DOGFOOD_DISABLE=1` to suppress all auto-emission.

## Run header

| Field | Value |
| --- | --- |
| Operator | _set on `tessera dogfood init playbook --operator …`_ |
| Start date | _from `gate_initialized.start_date`_ |
| End date | _from `gate_completed.end_date`_ |
| Tessera version | _stored on every ledger row_ |
| Vault schema version | _pass via `--field vault_schema_version=4` on init_ |
| Vault facet count at start | _pass via `--field vault_facet_count_at_start=…`_ |
| Targets in scope | _pass via `--field targets_in_scope=…`_ |
| Recipes in use | _captured per-register via `compiler_version`_ |
| Audit-verify cadence | per compile + per staleness event + weekly |

## Evidence log

Auto-generated from `~/.tessera/dogfood/playbook.jsonl`. Run `tessera dogfood render playbook` to refresh.

<!-- BEGIN tessera-dogfood evidence-log -->
| Date (UTC) | Machine | Kind | Target | External ID | Compiler version | Exit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _no records yet_ | | | | | | | |
<!-- END tessera-dogfood evidence-log -->

## Failure cases

Failure entries are mandatory. The Phase 9 task list calls out four classes the gate must surface explicitly; an entry stating "not observed" is acceptable, but absence of the section is not. Each `failure_case` ledger row appears in the Evidence Log above; the table below is operator narrative.

| Class | Description | Status |
| --- | --- | --- |
| Stale artifact trusted accidentally | An agent or human used the artifact as authoritative without noticing `is_stale=true`. | Pending |
| Source missing from compiled output | A source facet enumerated by `tessera playbook sources <target>` did not appear in `## Source inventory` after the compile. | Pending |
| Eval passed but answer was still weak | All `must`/`should` evals scored pass yet the artifact missed a recurring question the operator actually needed. | Pending |
| Artifact too lossy for exploratory use | Operator fell back to raw `recall` for a question the Playbook should have served, exposing a scoping mistake or over-eager compile. | Pending |

Each entry, when populated, lists the target, the date, the compile that produced the failure, the recipe `compiler-version`, the corrective action (retire target, recompile with new sources, expand brief, etc.), and any upstream change required in the recipe pack or CLI.

## Ranking-penalty decision

`.docs/compiled-playbooks-enhancement-plan.md §Phase 4` left the stale-artifact ranking-penalty as an open decision. The default is **no penalty, loud metadata**; the dogfood gate is the trigger for revisiting it.

Resolve the decision with one of:

- **No penalty, loud metadata (default).** Record at least three observed cases where the V0.5-P7 `is_stale` warning plus the `compiled_artifact_stale` recall annotation kept the operator from trusting a stale artifact. The recommendation stays "no closed-form penalty in retrieval; surface staleness loudly through metadata."
- **Closed-form penalty.** Record at least three observed cases where the warning was insufficient — the artifact ranked high enough that an agent or human used it before reading the warning. Pair the recommendation with a target multiplier or score offset and link to the proposed retrieval patch.

Land the recommendation as a `decision` ledger row (`--kind decision --field decision_id=ranking_penalty --field recommendation=…`) when the run completes; do not edit the Phase 4 plan section until the gate surfaces enough evidence.

| Decision | Status |
| --- | --- |
| Recommendation | Pending |
| Cases supporting recommendation | Pending |
| Linked retrieval patch (if any) | Pending |

## Acceptance summary

Auto-generated from the ledger; the gate clears when every row reads `Met`. The `Integrity blockers closed` row is manual — the operator records the sign-off via `tessera dogfood record playbook --kind note` once outstanding bugs are closed.

<!-- BEGIN tessera-dogfood acceptance-summary -->
| Check | Status | Evidence |
| --- | --- | --- |
| Two or more Phase 9 targets registered | Pending | — |
| Register and recompile both driven through the shipped CLI | Pending | register=False, recompile=False |
| Source mutation triggered staleness through `mark_stale_for_source` | Pending | — |
| Recompile produced fresh artifact preserving `target` | Pending | — |
| `tessera audit verify` passed at every checkpoint | Pending | audit_verify with non-zero exit or no rows |
| Failure-case log populated for every class | Pending | classes logged:  |
| Ranking-penalty decision recorded | Pending | — |
| Integrity blockers closed | Pending | manual sign-off required (record via `tessera dogfood record playbook --kind note`) |
<!-- END tessera-dogfood acceptance-summary -->

## Follow-up decision

If this run produces repeated cases of stale artifacts being trusted despite the loud-metadata default, document them under `## Ranking-penalty decision` before implementing the closed-form retrieval penalty. The default behavior remains "no penalty" unless the dogfood evidence shows the warning surface is not enough on its own.

If the run produces repeated requests for narrow field queries against MCP or REST callers (rather than the CLI), document them before promoting the V0.5-P7 artifact-query shape from CLI-only to a daemon surface. The CLI surface remains the v0.5 contract unless the dogfood evidence shows non-CLI callers need the same shape.
