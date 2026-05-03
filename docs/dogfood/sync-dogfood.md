# Multi-Machine Sync Dogfood Evidence

**Status:** Pending external evidence.

This document tracks the v0.5 dogfood gate for continuous multi-machine sync. It
does not close the gate until a real user or real second-machine workflow has
run Tessera sync continuously for at least 30 days.

## Gate

The gate is complete only when all of the following are true:

- One named operator uses the same encrypted Tessera vault from at least two
  machines.
- The run lasts at least 30 consecutive calendar days.
- Sync is exercised through the shipped `tessera sync push` and
  `tessera sync pull` paths against the configured backend.
- At least one post-pull `tessera audit verify` succeeds on each machine.
- The run records any observed divergence, replay rejection, conflict, stalled
  sync, credential failure, daemon responsiveness issue, or restore failure.
- No unresolved data-loss, audit-chain, or sync-integrity bug remains open at
  the end of the run.

Synthetic local snapshot load is not enough for this gate. The 50K-facet local
snapshot run is tracked separately by `B-SYNC-1` under
`docs/benchmarks/B-SYNC-1-snapshot-load/results/`.

## Run Protocol

Record the following before the run starts:

| Field | Value |
| --- | --- |
| Operator | Pending |
| Start date | Pending |
| End date | Pending |
| Machine A | Pending |
| Machine B | Pending |
| Tessera version | Pending |
| Vault schema version | Pending |
| Sync backend | Pending |
| Object bucket or filesystem target | Pending |
| Master-key handling | Pending |

Daily or per-sync notes should capture:

- command run
- machine name
- sync direction
- manifest sequence before and after the command
- elapsed time
- vault size
- facet count
- audit verification result
- failure details, if any

## Evidence Log

| Date | Machine | Command | Result | Notes |
| --- | --- | --- | --- | --- |
| Pending | Pending | Pending | Pending | Pending |

## Acceptance Summary

| Check | Status |
| --- | --- |
| 30 consecutive days completed | Pending |
| Two-machine workflow used | Pending |
| Push and pull both exercised | Pending |
| Audit verification passed after pull | Pending |
| Sync failures documented | Pending |
| Data-loss or sync-integrity blockers closed | Pending |

## Follow-Up Decision

If this run produces real conflict cases, document them before implementing
row-level merge behavior. Snapshot sync remains the v0.5 behavior unless the
dogfood evidence shows unacceptable lost work or manual merge friction.
