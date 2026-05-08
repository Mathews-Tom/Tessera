# Multi-Machine Sync Dogfood Evidence

**Status:** Pending external evidence.

This document tracks the v0.5 dogfood gate for continuous multi-machine sync. It does not close the gate until a real user or real second-machine workflow has run Tessera sync continuously for at least 30 days.

## Gate

The gate is complete only when all of the following are true:

- One named operator uses the same encrypted Tessera vault from at least two machines.
- The run lasts at least 30 consecutive calendar days.
- Sync is exercised through the shipped `tessera sync push` and `tessera sync pull` paths against the configured backend.
- At least one post-pull `tessera audit verify` succeeds on each machine.
- The run records any observed divergence, replay rejection, conflict, stalled sync, credential failure, daemon responsiveness issue, or restore failure.
- No unresolved data-loss, audit-chain, or sync-integrity bug remains open at the end of the run.

Synthetic local snapshot load is not enough for this gate. The 50K-facet local snapshot run is tracked separately by `B-SYNC-1` under `docs/benchmarks/B-SYNC-1-snapshot-load/results/`.

## Recording protocol

Evidence accrues in the JSONL ledger at `~/.tessera/dogfood/sync.jsonl` (override with `$TESSERA_DOGFOOD_DIR`). Every row carries a real `machine_id`, real timestamp, and the Tessera version that emitted it. Synthetic rows are not allowed; the ledger is append-only and the `tessera audit verify` + `tessera sync push|pull` commands auto-emit one row each per real invocation when the gate is active.

Open the gate before the run starts:

```bash
tessera dogfood init sync \
  --operator "Tom Mathews" \
  --start-date 2026-05-09 \
  --field machine_a=macbook-pro-m1.local \
  --field machine_b=linux-desktop.local \
  --field sync_backend=s3 \
  --field object_target=s3://bucket/tessera \
  --field vault_schema_version=4
```

Run normal sync work; rows auto-append:

```bash
tessera sync push          # emits sync_op (push) to the sync ledger
tessera sync pull          # emits sync_op (pull) to the sync ledger
tessera audit verify       # emits audit_verify to every active gate
```

Record context the structured kinds do not carry:

```bash
tessera dogfood record sync --kind note \
  --field text="manifest sequence 17 took 38 s on coffee-shop wifi"
```

Re-render the published evidence-log + acceptance-summary tables from the ledger:

```bash
tessera dogfood render sync           # rewrites this doc between markers
tessera dogfood render sync --no-write   # prints without writing
```

Close the gate when the run ends:

```bash
tessera dogfood record sync --kind gate_completed \
  --field end_date=2026-06-12 \
  --field outcome=clean
```

Set `TESSERA_DOGFOOD_DISABLE=1` to suppress all auto-emission (the manual `tessera dogfood` commands also refuse to write under this flag, so the operator is not split between two ledgers).

## Run header

| Field | Value |
| --- | --- |
| Operator | _set on `tessera dogfood init sync --operator …`_ |
| Start date | _from `gate_initialized.start_date`_ |
| End date | _from `gate_completed.end_date`_ |
| Tessera version | _stored on every ledger row_ |
| Vault schema version | _pass via `--field vault_schema_version=4` on init_ |
| Sync backend | _pass via `--field sync_backend=s3`_ |
| Object bucket or filesystem target | _pass via `--field object_target=…`_ |

The `Run protocol` checklist that previously lived here is now encoded as the `tessera dogfood render` predicates: every required signal (push + pull both exercised, post-pull audit success, distinct machines, failure documentation) is a deterministic predicate over the ledger contents and shows up under **Acceptance Summary** below.

## Evidence log

Auto-generated from `~/.tessera/dogfood/sync.jsonl`. Run `tessera dogfood render sync` to refresh.

<!-- BEGIN tessera-dogfood evidence-log -->
| Date (UTC) | Machine | Kind | Command | Seq Δ | Elapsed (ms) | Exit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _no records yet_ | | | | | | | |
<!-- END tessera-dogfood evidence-log -->

## Acceptance summary

Auto-generated from the same ledger; the gate clears when every row reads `Met`. The `Integrity blockers closed` row is intentionally manual — the operator records the sign-off via `tessera dogfood record sync --kind note` once outstanding bugs are closed.

<!-- BEGIN tessera-dogfood acceptance-summary -->
| Check | Status | Evidence |
| --- | --- | --- |
| 30 consecutive days completed | Pending | no gate_initialized row |
| Two-machine workflow used | Pending | — |
| Push and pull both exercised | Pending | push=False, pull=False |
| Audit verification passed after pull | Pending | machines without passing audit_verify: — |
| Sync failures documented | Pending | no gate_initialized row |
| Data-loss or sync-integrity blockers closed | Pending | manual sign-off required (record via `tessera dogfood record sync --kind note`) |
<!-- END tessera-dogfood acceptance-summary -->

## Follow-up decision

If this run produces real conflict cases, document them before implementing row-level merge behavior. Snapshot sync remains the v0.5 behavior unless the dogfood evidence shows unacceptable lost work or manual merge friction.
