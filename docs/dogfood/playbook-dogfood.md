# Playbook Dogfood Evidence

**Status:** Pending external evidence.

This document tracks the Phase 9 dogfood gate from
`.docs/compiled-playbooks-enhancement-plan.md` for task-shaped Playbooks. It does
not close the gate until at least one named operator has compiled, recompiled,
and used real Playbooks against a working Tessera vault long enough to surface
representative failure cases. The companion `docs/dogfood/compiled-notebook-dogfood.md`
gates the v0.5 write-time-compilation DoD bullet at the artifact level; this
document gates the task-shaped Playbook surface (compile target descriptors,
eval-set conventions, field provenance, the `tessera playbook` CLI, and the
recipe pack) shipped in PRs #73–#82.

## Gate

The gate is complete only when all of the following are true:

- At least two of the four Phase 9 dogfood targets are registered as real
  Playbooks against a working Tessera vault.
- One named operator drives every compile, recompile, and source mutation
  through the shipped CLI surface (`tessera playbook scaffold`,
  `tessera playbook register`, `tessera playbook stale`, and
  `tessera playbook inspect`); no compiled artifact is registered through
  hand-written SQL or the Python API for the run.
- Each registered artifact carries the seven minimum sections from
  `docs/playbook-compiler-recipes.md §Minimum artifact sections`, the
  recipe-pack `compiler-version` string, and a non-empty `## Eval summary`
  block.
- At least one source facet referenced by a registered artifact mutates during
  the run; `tessera playbook stale --json` lists the affected `external_id`
  and the cascade cause is recorded in the evidence log.
- The stale artifact is recompiled through the same recipe; the new artifact
  carries the same `metadata.target` value, ships with `is_stale=false`, and
  becomes the most-recent-fresh candidate for `[[playbook:<target>]]` and
  target-keyed lookups. The previous artifact stays in storage with
  `is_stale=true` and remains inspectable through `tessera playbook inspect
  <ulid>` until it is explicitly forgotten or compacted.
- `tessera audit verify` returns exit 0 on the vault before the run, after the
  first `register`, after the staleness flip, and after the recompile.
- Failure cases are captured verbatim under the `## Failure cases` log, even
  when the run otherwise succeeds. An empty failure log is a recipe smell, not
  a clean run.
- The ranking-penalty decision (Phase 4 open question) is resolved with a
  recorded recommendation: keep "no penalty, loud metadata" or land the
  closed-form ranking adjustment described in
  `.docs/compiled-playbooks-enhancement-plan.md §Phase 4`.
- No unresolved data-loss, audit-chain, provenance, or compiled-artifact
  integrity bug remains open at the end of the run.

Synthetic unit tests, throwaway demo artifacts, and the recipe-pack examples
do not satisfy this gate. Implementation behavior is covered by the
compiled-artifact, staleness, recall-surface, and audit-chain test suites; this
document tracks the longitudinal product evidence those tests cannot supply.

## Dogfood targets

The four Phase 9 targets, each tied to one recurring task. The gate clears at
two registered targets; the remaining two stay queued for follow-on dogfood.

| Target | Recurring task | Success criterion | Recipe |
| --- | --- | --- | --- |
| `tessera_release_playbook` | execute release prep consistently | reduces release-context gathering and catches missing gates before a `vX.Y.0rcN` cut | `claude-code/release-recipe@YYYY-MM-DD` |
| `swcr_design_brief` | answer SWCR architecture and retrieval-design questions | answers representative design questions with source-backed claims and no invented ULIDs | `claude-code/swcr-recipe@YYYY-MM-DD` |
| `dissertation_memory_chapter` | maintain vertical-depth research context | remains useful for the operator's research work across 30+ active days | `manual/research-recipe@<semver-or-date>` |
| `project_context_adapter_brief` | guide v0.6 project-context-layer design | clarifies source refs, integrity checks, and `tessera expand` decisions for the v0.6 scope | `claude-code/context-recipe@YYYY-MM-DD` |

Recipe identifiers follow the runner-name / recipe-name / version convention
from `docs/playbook-compiler-recipes.md §Compiler version naming`. The dates
above are placeholders; the operator stamps the active date on each compile.

## Run protocol

Record the following before the run starts:

| Field | Value |
| --- | --- |
| Operator | Pending |
| Start date | Pending |
| End date | Pending |
| Tessera version | Pending |
| Vault schema version | Pending |
| Vault facet count at start | Pending |
| Targets in scope | Pending |
| Recipes in use | Pending |
| Audit-verify cadence | Per compile + per staleness event + weekly |

Each compile loop captures the following per target. The recipe pack treats
the artifact body as the Markdown source of truth; the evidence log captures
the operational trace, not the artifact content.

| Field | Notes |
| --- | --- |
| `target` | Compile target descriptor `target` value. |
| `compiler-version` | The exact runner-name / recipe-name / version stamp. |
| Source ULIDs | Output of `tessera playbook sources <target>` at compile time. |
| Source facet count | Mirrors the source-ULID list. |
| Brief command | The `tessera playbook scaffold` invocation. |
| Compile elapsed | Wall-clock time from brief generation to artifact body finalization. |
| `register` exit | Exit code and `external_id` written by `tessera playbook register`. |
| Eval summary | Counts of `must`/`should`/`exploratory` passes, fails, skips. Verbatim `must` failure detail copied from the artifact body. |
| `tessera audit verify` | Exit code recorded after registration. |
| `tessera playbook inspect` | At least one field-level lookup against the artifact (`--field "Eval summary"` plus one operator-chosen field) confirms the artifact reads back correctly. |
| Cross-recall | A `tessera recall` call covering one of the eval questions confirms the artifact appears with `mode=write_time` and `is_stale=false`. |
| Repeat-task savings | Subjective minutes saved on the next time the recurring task runs against the artifact. |

Each staleness loop captures:

| Field | Notes |
| --- | --- |
| Trigger | Source facet ULID and mutation kind (`capture` un-delete, `forget`/`compaction`, `skill update`, etc.). |
| `tessera playbook stale --json` | Pre- and post-mutation snapshots; the cascade cause from the audit row is preserved. |
| Recall behavior | A `tessera recall` call confirms the stale artifact still surfaces with `is_stale=true` and the `compiled_artifact_stale` warning. |
| Recompile elapsed | Wall-clock time from `stale` detection to the new artifact's `register` exit. |
| Recompile diff | One-paragraph note on what changed in the artifact body and whether the eval summary moved. |
| `tessera audit verify` | Exit code after the staleness flip and after the recompile. |
| Old-artifact disposition | The stale artifact stays in storage with `is_stale=true` after the recompile and continues to surface in `recall` with the V0.5-P7 stale annotation; record whether the operator explicitly forgets or compacts it, when that happens, and any audit-chain row produced by the disposition. The new fresh artifact becomes the most-recent-fresh candidate for `[[playbook:<target>]]` and target-keyed lookups regardless of the old artifact's disposition. |

## Evidence log

| Date | Target | Compiler version | Sources | Audit verify | Recall result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Pending | Pending | Pending | Pending | Pending | Pending | Pending |

## Failure cases

Failure entries are mandatory. The Phase 9 task list calls out four classes
the gate must surface explicitly; an entry stating "not observed" is
acceptable, but absence of the section is not.

| Class | Description | Status |
| --- | --- | --- |
| Stale artifact trusted accidentally | An agent or human used the artifact as authoritative without noticing `is_stale=true`. | Pending |
| Source missing from compiled output | A source facet enumerated by `tessera playbook sources <target>` did not appear in `## Source inventory` after the compile. | Pending |
| Eval passed but answer was still weak | All `must`/`should` evals scored pass yet the artifact missed a recurring question the operator actually needed. | Pending |
| Artifact too lossy for exploratory use | Operator fell back to raw `recall` for a question the Playbook should have served, exposing a scoping mistake or over-eager compile. | Pending |

Each entry, when populated, lists the target, the date, the compile that
produced the failure, the recipe `compiler-version`, the corrective action
(retire target, recompile with new sources, expand brief, etc.), and any
upstream change required in the recipe pack or CLI.

## Ranking-penalty decision

`.docs/compiled-playbooks-enhancement-plan.md §Phase 4` left the stale-artifact
ranking-penalty as an open decision. The default is **no penalty, loud
metadata**; the dogfood gate is the trigger for revisiting it.

Resolve the decision with one of:

- **No penalty, loud metadata (default).** Record at least three observed
  cases where the V0.5-P7 `is_stale` warning plus the `compiled_artifact_stale`
  recall annotation kept the operator from trusting a stale artifact. The
  recommendation stays "no closed-form penalty in retrieval; surface
  staleness loudly through metadata."
- **Closed-form penalty.** Record at least three observed cases where the
  warning was insufficient — the artifact ranked high enough that an agent
  or human used it before reading the warning. Pair the recommendation with
  a target multiplier or score offset and link to the proposed retrieval
  patch.

The recommendation lands here as a verbatim block when the run completes; do
not edit the Phase 4 plan section until this gate surfaces enough evidence.

| Decision | Status |
| --- | --- |
| Recommendation | Pending |
| Cases supporting recommendation | Pending |
| Linked retrieval patch (if any) | Pending |

## Acceptance summary

| Check | Status |
| --- | --- |
| Two or more Phase 9 targets registered | Pending |
| Compile and recompile both driven through the shipped CLI | Pending |
| Source mutation triggered staleness through `mark_stale_for_source` | Pending |
| Stale artifact remained inspectable while marked stale | Pending |
| Recompile produced fresh artifact preserving `target` | Pending |
| `tessera audit verify` passed at every checkpoint | Pending |
| Failure-case log populated for every class | Pending |
| Ranking-penalty decision recorded | Pending |
| Integrity blockers closed | Pending |

## Follow-up decision

If this run produces repeated cases of stale artifacts being trusted despite
the loud-metadata default, document them under `## Ranking-penalty decision`
before implementing the closed-form retrieval penalty. The default behavior
remains "no penalty" unless the dogfood evidence shows the warning surface is
not enough on its own.

If the run produces repeated requests for narrow field queries against MCP or
REST callers (rather than the CLI), document them before promoting the V0.5-P7
artifact-query shape from CLI-only to a daemon surface. The CLI surface
remains the v0.5 contract unless the dogfood evidence shows non-CLI callers
need the same shape.
