# Compiled-Notebook Dogfood Evidence

**Status:** Pending external evidence.

This document tracks the v0.5 dogfood gate for write-time compilation on a real
research topic. It does not close the gate until Tom's actual dissertation
research has been represented as a `compiled_notebook` for at least 60 days and
the synthesized output has proved useful in real work.

## Gate

The gate is complete only when all of the following are true:

- Tom uses one real dissertation research topic as the source material.
- Tessera stores the synthesized artifact as a `compiled_notebook` through the
  shipped compiled-artifact registration path.
- The run lasts at least 60 consecutive calendar days.
- Source facets are updated during the run, and stale detection marks affected
  compiled artifacts without corrupting the audit chain.
- Interrupted compilation or registration attempts are retried without duplicate
  active artifacts, lost provenance, or audit-chain failure.
- The compiled output is reviewed against the source facets and judged useful
  for actual research work, not only syntactically valid.
- No unresolved data-loss, audit-chain, provenance, or compiled-artifact
  integrity bug remains open at the end of the run.

Synthetic unit tests and one-off demo artifacts are not enough for this gate.
The implementation-level behavior is covered by the compiled-artifact,
staleness, recall-surface, and audit-chain test suites; this document tracks the
long-running product evidence those tests cannot supply.

## Run Protocol

Record the following before the run starts:

| Field | Value |
| --- | --- |
| Operator | Pending |
| Start date | Pending |
| End date | Pending |
| Tessera version | Pending |
| Vault schema version | Pending |
| Research topic | Pending |
| Source facet types | Pending |
| Source facet count | Pending |
| Compiled artifact external ID | Pending |
| Compiler or calling agent version | Pending |
| Review cadence | Pending |

Each review entry should capture:

- date
- command or API path used
- source facet count
- compiled artifact external ID
- compiler version
- stale flag before and after source edits
- `tessera audit verify` result
- whether the output changed after new source material
- usefulness assessment for the current research task
- failure details, if any

## Evidence Log

| Date | Source count | Command or API path | Audit result | Usefulness | Notes |
| --- | --- | --- | --- | --- | --- |
| Pending | Pending | Pending | Pending | Pending | Pending |

## Acceptance Summary

| Check | Status |
| --- | --- |
| 60 consecutive days completed | Pending |
| Real dissertation topic used | Pending |
| Compiled artifact registered through shipped path | Pending |
| Source updates exercised stale detection | Pending |
| Interrupted compilation or retry behavior verified | Pending |
| Audit verification passed after compiled-artifact changes | Pending |
| Output judged useful for real research work | Pending |
| Integrity blockers closed | Pending |

## Follow-Up Decision

If this run produces repeated temporal questions such as "what was I thinking
about this project two weeks ago?", document those cases before implementing
episodic temporal retrieval. Ordinary SWCR recall and compiled artifacts remain
the v0.5 behavior unless the dogfood evidence shows a concrete temporal-recall
gap.
