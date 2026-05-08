# Compiled-Notebook Dogfood Evidence

**Status:** Pending external evidence.

This document tracks the v0.5 dogfood gate for write-time compilation on a real research topic. It does not close the gate until Tom's actual dissertation research has been represented as a `compiled_notebook` for at least 60 days and the synthesized output has proved useful in real work.

## Gate

The gate is complete only when all of the following are true:

- Tom uses one real dissertation research topic as the source material.
- Tessera stores the synthesized artifact as a `compiled_notebook` through the shipped compiled-artifact registration path.
- The run lasts at least 60 consecutive calendar days.
- Source facets are updated during the run, and stale detection marks affected compiled artifacts without corrupting the audit chain.
- Interrupted compilation or registration attempts are retried without duplicate active artifacts, lost provenance, or audit-chain failure.
- The compiled output is reviewed against the source facets and judged useful for actual research work, not only syntactically valid.
- No unresolved data-loss, audit-chain, provenance, or compiled-artifact integrity bug remains open at the end of the run.

Synthetic unit tests and one-off demo artifacts are not enough for this gate. The implementation-level behavior is covered by the compiled-artifact, staleness, recall-surface, and audit-chain test suites; this document tracks the long-running product evidence those tests cannot supply.

## Recording protocol

Evidence accrues in the JSONL ledger at `~/.tessera/dogfood/compiled.jsonl` (override with `$TESSERA_DOGFOOD_DIR`). Every row carries a real `machine_id`, real timestamp, and the Tessera version that emitted it. Synthetic rows are not allowed; the ledger is append-only and the relevant CLI commands auto-emit one row each per real invocation when the gate is active.

Auto-emitting commands once `tessera dogfood init compiled` has run:

| Command | Auto-emitted kind | Notes |
| --- | --- | --- |
| `tessera playbook register` | `register` | target + external_id + compiler_version + source_count + exit_code + elapsed_ms |
| `tessera playbook stale` | `stale_event` | most recent cascade cause + total stale count (only when the listing is non-empty) |
| `tessera audit verify` | `audit_verify` | exit_code + outcome (`intact` / `empty_chain` / `broken_row` / `schema_error`) |

Open the gate before the run starts:

```bash
tessera dogfood init compiled \
  --operator "Tom Mathews" \
  --start-date 2026-05-09 \
  --field research_topic="annealed memory consolidation in agentic systems" \
  --field source_facet_types=project,skill,verification_checklist \
  --field vault_schema_version=4 \
  --field compiler_version=manual/research-recipe@2026-05-09
```

Run normal compile work; rows auto-append:

```bash
tessera playbook register dissertation_memory_chapter \
  --content notebook.md --compiler-version manual/research-recipe@2026-05-09
tessera playbook stale            # emits stale_event when the listing is non-empty
tessera audit verify              # emits audit_verify to every active gate
```

Record subjective signal the structured kinds do not carry. The `compiled` gate admits a `review` kind for the usefulness call, plus `note` for narrative context:

```bash
tessera dogfood record compiled --kind review \
  --field usefulness=high \
  --field reviewed_external_id=01H... \
  --field text="answered the question I came in with on the first read"

tessera dogfood record compiled --kind note \
  --field text="recompile after Sept-15 capture broadened the SWCR section"
```

Re-render the published evidence-log and acceptance-summary tables:

```bash
tessera dogfood render compiled            # rewrites this doc between markers
tessera dogfood render compiled --no-write   # prints without writing
```

Close the gate when the run ends:

```bash
tessera dogfood record compiled --kind gate_completed \
  --field end_date=2026-07-08 \
  --field outcome=clean
```

Set `TESSERA_DOGFOOD_DISABLE=1` to suppress all auto-emission.

## Run header

| Field | Value |
| --- | --- |
| Operator | _set on `tessera dogfood init compiled --operator …`_ |
| Start date | _from `gate_initialized.start_date`_ |
| End date | _from `gate_completed.end_date`_ |
| Tessera version | _stored on every ledger row_ |
| Vault schema version | _pass via `--field vault_schema_version=4` on init_ |
| Research topic | _pass via `--field research_topic=…` on init (drives the `real_topic` predicate)_ |
| Source facet types | _pass via `--field source_facet_types=…`_ |
| Compiled artifact external ID | _emitted on every `register` ledger row_ |
| Compiler or calling agent version | _pass via `--field compiler_version=…` on init and on every register_ |

Each review entry should still capture date, command or API path used, source facet count, compiled artifact external_id, compiler version, stale flag before and after source edits, `tessera audit verify` result, whether the output changed after new source material, usefulness assessment, and failure details. The `register` / `stale_event` / `audit_verify` rows carry the structured fields; `review` rows carry the subjective verdict; the markdown table below is the operator-readable rollup.

## Evidence log

Auto-generated from `~/.tessera/dogfood/compiled.jsonl`. Run `tessera dogfood render compiled` to refresh.

<!-- BEGIN tessera-dogfood evidence-log -->
| Date (UTC) | Machine | Kind | External ID | Compiler version | Elapsed (ms) | Exit / Useful | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _no records yet_ | | | | | | | |
<!-- END tessera-dogfood evidence-log -->

## Acceptance summary

Auto-generated from the same ledger; the gate clears when every row reads `Met`. The `Integrity blockers closed` row is intentionally manual — the operator records the sign-off via `tessera dogfood record compiled --kind note` once outstanding bugs are closed.

<!-- BEGIN tessera-dogfood acceptance-summary -->
| Check | Status | Evidence |
| --- | --- | --- |
| 60 consecutive days completed | Pending | no gate_initialized row |
| Real dissertation topic used | Pending | — |
| Compiled artifact registered through shipped path | Pending | — |
| Source updates exercised stale detection | Pending | — |
| Audit verification passed after compiled-artifact changes | Pending | — |
| Output judged useful for real research work | Pending | — |
| Integrity blockers closed | Pending | manual sign-off required (record via `tessera dogfood record compiled --kind note`) |
<!-- END tessera-dogfood acceptance-summary -->

## Follow-up decision

If this run produces repeated temporal questions such as "what was I thinking about this project two weeks ago?", document those cases before implementing episodic temporal retrieval. Ordinary SWCR recall and compiled artifacts remain the v0.5 behavior unless the dogfood evidence shows a concrete temporal-recall gap.
