# Tessera — Vault Migration Contract

**Status:** Draft 1
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## Why this document exists

Schema migrations are the most dangerous category of change in a local-first product. The user's vault is the product; a broken migration is the product failing. Tessera makes a promise that vault state is durable across upgrades. This document specifies what that promise means operationally, and what the user is guaranteed when it breaks.

## Invariants

Across every migration, every version:

1. **No migration proceeds without a successful pre-migration snapshot.** The user always has a path back.
2. **Schema version is a single atomic commit.** The vault is never "half v0.3".
3. **Partial failure is safe.** A crash mid-migration leaves the vault in a state the next daemon start can diagnose and offer to repair or rollback.
4. **Rollback is explicit, named, and user-run.** The daemon never auto-rolls-back.
5. **Migrations are idempotent on re-run.** If the first attempt wrote half the changes and the second attempt runs the same script, the final state is correct.

## Version state machine

```
 installed binary version      vault schema version
          v_bin                        v_vault

 Case A:  v_bin >  v_vault         → forward migration required
 Case B:  v_bin == v_vault         → normal operation
 Case C:  v_bin <  v_vault         → refuse to start; user has a newer vault than binary
 Case D:  v_vault is in-transit    → refuse to start; offer rollback or resume
```

### States recorded in `_meta`

| Key                    | Value             | Meaning                                                  |
| ---------------------- | ----------------- | -------------------------------------------------------- |
| `schema_version`       | integer           | Last fully-applied schema version                        |
| `schema_target`        | integer or NULL   | If non-null, a migration is in progress to this version  |
| `migration_started_at` | timestamp or NULL | When the in-progress migration began                     |
| `kdf_version`          | integer           | Argon2id parameter set version (see §Encryption at rest) |
| `vault_id`             | ULID              | Stable identifier for this vault, set once at init       |

Entering a migration: `schema_target` and `migration_started_at` are set in a transaction before any schema change. Exiting a successful migration: `schema_version` is bumped, `schema_target` and `migration_started_at` are cleared, all in a single transaction.

A non-null `schema_target` on daemon start means Case D — migration was interrupted.

## Forward migration

```mermaid
sequenceDiagram
  participant User
  participant CLI
  participant Daemon
  participant Vault
  participant Backup

  User->>CLI: tessera daemon start
  CLI->>Daemon: spawn
  Daemon->>Vault: read schema_version
  Vault-->>Daemon: v0.1
  Daemon->>Daemon: binary is v0.3 → forward migration
  Daemon->>User: prompt: migrate v0.1 → v0.3? (y/N)
  User-->>Daemon: y
  Daemon->>Backup: copy vault.db → vault.db.pre-v0.3-<timestamp>
  Backup-->>Daemon: ok
  Daemon->>Vault: BEGIN; set schema_target=3, migration_started_at=now
  Daemon->>Vault: apply migration script v0.1→v0.3 (DDL + data)
  Daemon->>Vault: validation queries (row counts, FK integrity)
  Vault-->>Daemon: ok
  Daemon->>Vault: set schema_version=3, schema_target=NULL, COMMIT
  Daemon-->>User: ✓ migrated to v0.3; backup at vault.db.pre-v0.3-<ts>
  Daemon->>Daemon: proceed to normal startup
```

### Rules

- **Pre-migration backup is mandatory.** Cannot be disabled. Copied (not hardlinked) to `vault.db.pre-v<version>-<timestamp>` in the same directory.
- **User consent is mandatory for non-patch migrations.** Patch-level migrations (pure additive indexes, comments) are auto-applied; schema-affecting migrations require `y` at the prompt, or `--yes` on a one-shot CLI invocation.
- **Validation queries run after DDL, before the commit.** Row counts per affected table are compared to pre-migration snapshots; foreign keys verified with `PRAGMA foreign_key_check`.
- **The whole migration is one transaction where possible.** SQLite DDL is transactional for `CREATE`, `ALTER` (limited), and `INSERT`. Where a sequence is not atomic (e.g., `DROP`+`CREATE` for table restructuring), the sequence is guarded by `schema_target` and the next-boot repair path.

## Interrupted migration (Case D)

On daemon start with `schema_target IS NOT NULL`:

1. Daemon does **not** start serving MCP.
2. Emits event `migration_interrupted` with `schema_target`, `migration_started_at`, elapsed time.
3. Prompts user (or exposes `tessera vault recover` subcommand):

```
Vault is in-transit to schema version 3 (started 2026-04-14T09:22:15Z, 45 seconds ago).
Backup exists: vault.db.pre-v0.3-20260414T092215Z

Choose:
  [r] resume migration from current state
  [b] rollback: restore backup and abandon the migration
  [d] diagnose: run tessera vault inspect and exit
```

- **Resume** re-runs the migration script. Scripts are written idempotently (see §Idempotency).
- **Rollback** moves the current `vault.db` to `vault.db.aborted-v0.3-<ts>` and restores the backup, clears `schema_target`.
- **Diagnose** exits; user runs `tessera vault inspect` to see the current schema and decide.

### Never auto-resume

Auto-resume is tempting but dangerous: the interruption cause may be disk-full, OOM, or a bug in the migration script. Retrying without human review turns an interruption into data corruption. Require explicit choice.

## Rollback

```
tessera vault rollback [--to <backup-path>] [--dry-run]
```

- Stops the daemon.
- Verifies the backup integrity (SQLite `PRAGMA integrity_check`, schema version, vault_id matches).
- Moves the current vault to `vault.db.aborted-<ts>` (never deletes, in case of mistake).
- Restores backup as the active `vault.db`.
- Prints the schema version of the restored vault and exits.

Rollback is reversible: the aborted vault is kept until the user explicitly prunes.

## Idempotency

Every migration script is written as a sequence of operations each of which:

- Checks for the existence of the target structure before creating it (`CREATE TABLE IF NOT EXISTS`, `SELECT count FROM sqlite_master WHERE ...`).
- For data fills, checks whether the fill has already applied (`WHERE embed_model_id IS NULL` instead of `UPDATE everything`).
- Writes a per-step marker into a `_migration_steps` table so a partial run knows where to resume.

```sql
CREATE TABLE IF NOT EXISTS _migration_steps (
  schema_target INTEGER NOT NULL,
  step_name     TEXT NOT NULL,
  applied_at    INTEGER NOT NULL,
  PRIMARY KEY (schema_target, step_name)
);
```

Each script step is of the form:

```python
with db:
    already_applied = db.execute(
        "SELECT 1 FROM _migration_steps WHERE schema_target=? AND step_name=?",
        (target, step),
    ).fetchone()
    if already_applied:
        return
    # ... DDL / data operations ...
    db.execute(
        "INSERT INTO _migration_steps VALUES (?, ?, ?)",
        (target, step, now()),
    )
```

After a successful migration, `_migration_steps` rows for the prior target are deleted.

## Backup retention

- Pre-migration backups are kept by default.
- `tessera vault list-backups` enumerates them.
- `tessera vault prune-backups [--keep-last N] [--older-than <duration>]` removes; defaults to keep-last 3.
- Auto-prune runs on daemon start if total backup size exceeds 10× vault size; prompts user for confirmation.

## What is explicitly NOT offered

- **Automatic rollback on error.** The daemon does not decide to rollback on its own. Interrupted migrations halt and wait.
- **Cross-major-version skip migrations.** v0.1 → v1.0 directly is not supported; user must migrate through intermediate major versions.
- **Downgrade migrations.** Restore from backup is the downgrade path. No `v0.3 → v0.1` downgrade script ships.
- **Migration during active daemon.** The daemon takes an exclusive lock for the duration of the migration; no MCP calls are served.

## Schema v3 → v4 (cumulative across V0.5-P1 + V0.5-P2, ADRs 0016 / 0017)

Additive forward migration. Schema v4 absorbs the v0.5 reconciliation in cumulative form: every sub-phase that touches storage appends its own steps to the same `_V3_TO_V4_STEPS` list, and the schema bump stays at 4 because every delta is additive. The full v3 → v4 integration test runs end-to-end at v0.5 ship.

Steps registered in `_V3_TO_V4_STEPS` (in apply order):

1. `add_volatility_column` — `ALTER TABLE facets ADD COLUMN volatility TEXT NOT NULL DEFAULT 'persistent' CHECK (volatility IN ('persistent', 'session', 'ephemeral'))`. Guarded by a `PRAGMA table_info` check so resume replays cleanly. (V0.5-P1, ADR 0016.)
2. `add_ttl_seconds_column` — `ALTER TABLE facets ADD COLUMN ttl_seconds INTEGER`. Same guard. (V0.5-P1.)
3. `create_volatility_sweep_index` — partial index on `(volatility, captured_at)` filtering `is_deleted = 0 AND volatility IN ('session', 'ephemeral')`. Used by the daemon's idle-time compaction sweep so the common case (a vault dominated by persistent rows) sees no contention. (V0.5-P1.)
4. `extend_facets_facet_type_check` — extends the `facets.facet_type` CHECK constraint to reserve every v0.5 facet type (`agent_profile`, `verification_checklist`, `retrospective`, `automation`) alongside the existing `compiled_notebook` reservation. SQLite cannot modify a CHECK in place, so the step uses the standard 12-step table-recreate: drop FTS triggers, rename `facets` to `_facets_v3`, create the v4 facets table preserving every prior column (volatility, ttl_seconds, disk_path, mode, source_tool, embed metadata), copy rows verbatim, drop the staging table, recreate every facets-side index (including `facets_volatility_sweep`), refresh `facets_fts`, and reinstall the `facets_ai`/`facets_ad`/`facets_au` triggers. Idempotent by guard: re-runs return early when the live CHECK already lists `agent_profile`. (V0.5-P2, ADR 0017.)
5. `add_profile_facet_external_id` — `ALTER TABLE agents ADD COLUMN profile_facet_external_id TEXT REFERENCES facets(external_id) DEFERRABLE INITIALLY DEFERRED`. Nullable so existing tokens / agents that never registered a profile keep working unchanged; deferrable so `register_agent_profile` can insert the facet and update the agents row inside one transaction without the FK firing on the intermediate state. (V0.5-P2, ADR 0017.)

Each step is idempotent — a resume re-runs every step whose marker is absent, the savepoint pattern keeps the apply + marker-write atomic, and `IF NOT EXISTS` plus the column-presence / CHECK-presence checks prevent double-add errors. No backup is required for a column-add but the runner takes one anyway per the contract; the table-recreate step does the same work the v1 → v2 rebuild already did, with the same backup gate.

Forward, idempotent, and rollback tests live in `tests/unit/test_volatility.py::test_v3_to_v4_*` and `tests/unit/test_migration_runner.py::test_v3_to_v4_*` (P1 surfaces) plus `tests/unit/test_migration_runner.py::test_v3_to_v4_migration_extends_check_and_adds_agents_link` and `test_v3_to_v4_migration_is_idempotent_under_resume` (P2 surfaces).

## DoD for every migration

A migration ships only when:

1. Forward migration script is tested on a fixture vault of the previous schema.
2. An interrupted-migration test verifies recovery from a simulated crash at every `_migration_steps` checkpoint.
3. Backup-and-restore round-trip produces a vault indistinguishable from pre-migration state (compared via `.schema` and content-hash sums).
4. Documentation is updated with any new pre/post invariants.
5. `tessera vault inspect` recognizes the new schema version.

## Revisit triggers

- A user reports a migration that produced a corrupted vault despite the backup mechanism. Post-mortem required.
- Schema drift across many versions makes the single-transaction rule impractical. Consider staged migrations with explicit user confirmation per stage.
- Average migration wall time exceeds 5 minutes on real vaults. Design for background migration with read-only service during the window.
