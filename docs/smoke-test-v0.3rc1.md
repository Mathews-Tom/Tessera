# v0.3.0rc1 — Cross-Platform Smoke Test Runbook

**Status:** Open — v0.3.0rc1 → v0.3.0 GA stabilization gate, NOT an rc1 ship gate.
**Owner:** Tom Mathews
**Closes:** v0.1 DoD item 1 (cross-platform smoke) + v0.3 DoD cross-platform checkbox
**Decision recorded:** `docs/v0.1-dod-audit.md §Decision 2026-04-26`

> rc1 publishes on internal evidence (CI green, schema v3 migration covered by `tests/unit/test_migration_runner.py`, v0.3 surface covered by `tests/integration/test_mcp_tool_surface.py`). The recordings below happen during the rc1 → GA stabilization window, mirroring how v0.1 DoD items 1 and 9 rode along the v0.1.x → v0.5 stabilization window per the 2026-04-25 deferral decision.

---

## Purpose

Verify on clean VMs of macOS, Ubuntu, and Windows that:

1. `tessera-context==0.3.0rc1` installs from PyPI without manual platform fixes beyond what's documented in `docs/troubleshooting.md`.
2. `tessera init` → `tessera daemon start` → `tessera connect` → capture → recall completes the T-shape demo flow end-to-end in under 10 minutes (not counting Ollama model download).
3. The v2 → v3 migration runs cleanly on a populated rc2 vault: schema steps applied, all pre-migration facets preserved, new tables created, `tessera doctor` green.

Three platforms × two flows (clean install + rc2-upgrade) = six recorded runs.

---

## VM baselines

| Platform | OS image | Python | Notes |
|---|---|---|---|
| macOS | macOS 15.x on Apple Silicon | `brew install python@3.12` | sqlcipher via `brew install sqlcipher` per `docs/troubleshooting.md` |
| Ubuntu | Ubuntu 24.04 LTS server | system Python 3.12 if present, else `pyenv install 3.12` | `apt install build-essential libsqlcipher-dev` for sqlcipher3 wheel build |
| Windows | Windows 11 fresh install | python.org installer 3.12 | sqlcipher3 wheel availability is the platform risk; fall back to WSL2 Ubuntu if the wheel build fails and document the failure |

Each VM starts from a snapshot, runs one flow, gets reset. No accumulated state between flows.

---

## Flow A — Clean install (subsumes v0.1 DoD item 1)

Goal: a user with no prior Tessera install reaches a working `recall`.

```bash
# 1. Prerequisites
python3 --version          # must be 3.12.x
# macOS only:
brew install sqlcipher ollama
# Ubuntu only:
sudo apt install -y build-essential libsqlcipher-dev
curl -fsSL https://ollama.com/install.sh | sh
# Windows only: install Python 3.12 from python.org; install Ollama from ollama.com/download

ollama pull nomic-embed-text

# 2. Install the rc
pipx install --pip-args="--pre" tessera-context==0.3.0rc1
# or: pip install --pre tessera-context==0.3.0rc1
tessera --version          # expect 0.3.0rc1

# 3. Setup once — env vars drive the flag-free flow.
#    Vault path defaults to ~/.tessera/vault.db; passphrase comes from the env var.
export TESSERA_PASSPHRASE='smoke-test-passphrase'

# 4. Initialise
tessera init                       # creates ~/.tessera/vault.db
tessera daemon start
tessera doctor                     # all green

# 5. Wire one client
tessera connect claude-desktop --token-ttl-days 30

# 6. Capture and recall (CLI form, since Claude Desktop install is platform-variable)
tessera capture "I prefer uv over pip for Python." --facet-type preference
tessera capture "anneal — Artifact-Eval-Agent triplet, git worktrees for isolation." --facet-type project
tessera capture "$(printf 'Hook → Legend → Credibility Spike → Observation → Meaning. 150–300 words. No emojis.')" --facet-type workflow
tessera recall "LinkedIn post about anneal"

# 7. Stop daemon, capture timing
tessera daemon stop
```

**Pass criteria:**

- Every command exits 0.
- `tessera doctor` reports no red checks.
- `tessera recall` returns at least one facet from each captured type.
- Total wall time from step 2 to step 5 (excluding `ollama pull`): under 10 minutes.

**Capture for the recording:**

- Terminal output of every step (asciinema or platform equivalent).
- `tessera doctor` output.
- `tessera stats` output before stopping the daemon.
- One screenshot of the host OS confirming clean-VM identity.

---

## Flow B — v2 → v3 migration on a real rc2 vault

Goal: an existing rc2 user upgrades to v0.3.0rc1 without losing data.

```bash
# 1. Prerequisites: same as Flow A through ollama pull.

# 2. Install rc2 first
pipx install --pip-args="--pre" tessera-context==0.1.0rc2
tessera --version          # expect 0.1.0rc2
export TESSERA_PASSPHRASE='smoke-test-passphrase'
tessera init               # creates ~/.tessera/vault.db
tessera daemon start

# 3. Pre-seed the vault with at least 50 facets across all five v0.1 types.
#    Use the seed script (committed alongside this runbook):
bash docs/scripts/smoke-seed-rc2-vault.sh ~/.tessera/vault.db
tessera stats              # record per-type counts

# 4. Stop daemon, take a backup snapshot
tessera daemon stop
cp ~/.tessera/vault.db ~/.tessera/vault.pre-v3.db

# 5. Upgrade in place
pipx install --pip-args="--pre --force-reinstall" tessera-context==0.3.0rc1
tessera --version          # expect 0.3.0rc1

# 6. Daemon-start triggers the v2 → v3 migration (runner.py auto-applies on first connect)
tessera daemon start
tessera doctor

# 7. Verify migration
sqlite3 ~/.tessera/vault.db "SELECT target, applied_at FROM _migration_steps WHERE target=3"
sqlite3 ~/.tessera/vault.db "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('people','person_mentions')"
sqlite3 ~/.tessera/vault.db "PRAGMA table_info(facets)" | grep disk_path
tessera stats              # per-type counts unchanged from step 3

# 8. v0.3 surface comes alive
tessera skills list        # empty list, exits 0
tessera people list        # empty list, exits 0
```

**Pass criteria:**

- Every command exits 0.
- `_migration_steps` has rows with `target=3`.
- `people` and `person_mentions` tables present.
- `disk_path` column present on `facets`.
- Per-type facet counts in step 7 match step 3 exactly.
- Pre-migration backup file (`vault.pre-v3.db`) exists at the path the runner wrote it.
- `tessera doctor` green.

**Capture for the recording:**

- `tessera stats` output before and after migration (diff must be zero on counts).
- The `_migration_steps` row contents.
- Total wall time of the upgrade step.

---

## Failure modes to expect and how to handle them

| Symptom | Likely cause | Action |
|---|---|---|
| `sqlcipher3 ... fatal error: 'sqlcipher/sqlite3.h' file not found` on Linux | `libsqlcipher-dev` not installed | Install, retry; if Ubuntu < 24.04 has too-old sqlcipher, document the version floor in `docs/troubleshooting.md` |
| `sqlcipher3` wheel build fails on Windows | sqlcipher3 has no Windows wheels and source build needs Visual C++ | Fall back to WSL2 Ubuntu, record the failure as a known v0.3.x follow-up, do not block rc1 on a Windows-native fix |
| `ollama pull` 503s or hangs | Ollama daemon not running / network egress blocked in VM | Start Ollama daemon explicitly (`ollama serve` in a second shell on Linux); whitelist `ollama.com` egress |
| `tessera doctor` flags `port 5710 conflict` | Another process bound the port; stale daemon socket | `lsof -i :5710`; kill the offender or pick a different port via `--socket` |
| Migration step half-applies | sqlcipher key rotation / power loss mid-step | `runner.py` resume path picks up the same target; verify `_meta.schema_target=3` then re-run `tessera daemon start` |
| `tessera doctor` reports `Empty facet types` after migration | Pre-seed step didn't populate all five v0.1 types | Re-run the seed script before snapshotting the rc2 vault |

Any other failure that doesn't fit the table opens a GitHub issue and either gates the rc1 (if reproducible on a clean VM) or rides into v0.3.x (if platform-specific and worked around).

---

## Closing the gate

The gate is closed when:

- All three platforms have a recorded Flow A pass (or a documented fallback on Windows).
- All three platforms have a recorded Flow B pass.
- The recordings, terminal logs, and `_migration_steps` outputs are committed under `docs/benchmarks/B-SMOKE-1-v0.3rc1/` (one subdirectory per platform).
- The two checkboxes in `docs/release-spec.md §Definition of Done for v0.3` flip to `[x]` with the artifact paths.

The external-user demo (carry-over of v0.1 item 9) is a separate gate that closes between v0.3.0rc1 and v0.3.0 GA — it does not block rc1.
