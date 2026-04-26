# v0.4.0rc1 — Cross-Platform Smoke Test Runbook

**Status:** Open — v0.4.0rc1 → v0.4.0 GA stabilization gate, NOT an rc1 ship gate.
**Owner:** Tom Mathews
**Closes:** v0.1 DoD item 1 (cross-platform smoke) carried forward across the v0.3 → v0.4 ONNX stack switch
**Decision recorded:** `docs/v0.1-dod-audit.md §Decision 2026-04-26`

> rc1 publishes on internal evidence (CI green, full unit + integration suite passing under fastembed-mocked unit tests, end-to-end fastembed loaded once on the project author's macOS workstation). The recordings below happen during the rc1 → GA stabilization window.

---

## Purpose

Verify on clean VMs of macOS, Ubuntu, and Windows that:

1. `tessera-context==0.4.0rc1` installs from PyPI without manual platform fixes beyond what's documented in `docs/troubleshooting.md`.
2. `tessera init` → `tessera models set --activate` → `tessera daemon start` → `tessera connect` → capture → recall completes the T-shape demo flow end-to-end in under 10 minutes (not counting the one-time fastembed weight download).
3. The first daemon start downloads the embedder + reranker weights cleanly to `~/.cache/fastembed` and serves a `recall` against a freshly-captured facet.

Three platforms × one flow (clean install) = three recorded runs. No "rc → rc" upgrade flow exists in this cycle: the v0.3 → v0.4 boundary deletes Ollama and the sentence-transformers reranker entirely, so any rc2/rc3 vault must be re-embedded against fastembed weights — `tessera vault repair-embeds` plus a worker pass after `tessera models set --name <fastembed-id> --activate`. That migration is documented in `docs/troubleshooting.md` and not gated on a recording.

---

## VM baselines

| Platform | OS image | Python | Notes |
|---|---|---|---|
| macOS | macOS 15.x on Apple Silicon | `brew install python@3.12` | sqlcipher via `brew install sqlcipher` per `docs/troubleshooting.md`; fastembed picks the CoreML execution provider automatically |
| Ubuntu | Ubuntu 24.04 LTS server | system Python 3.12 if present, else `pyenv install 3.12` | `apt install build-essential libsqlcipher-dev` for sqlcipher3 wheel build; fastembed picks the CPU provider |
| Windows | Windows 11 fresh install | python.org installer 3.12 | sqlcipher3 wheel availability is the platform risk; fall back to WSL2 Ubuntu if the wheel build fails and document the failure |

Each VM starts from a snapshot, runs the flow, gets reset. No accumulated state between runs.

---

## Flow — Clean install

Goal: a user with no prior Tessera install reaches a working `recall`.

```bash
# 1. Prerequisites — sqlcipher headers only. No Ollama, no torch.
python3 --version          # must be 3.12.x
# macOS only:
brew install sqlcipher
# Ubuntu only:
sudo apt install -y build-essential libsqlcipher-dev
# Windows only: install Python 3.12 from python.org

# 2. Install the rc
pipx install --pip-args="--pre" tessera-context==0.4.0rc1
# or: pip install --pre tessera-context==0.4.0rc1
tessera --version          # expect 0.4.0rc1

# 3. Setup once — env vars drive the flag-free flow.
#    Vault path defaults to ~/.tessera/vault.db; passphrase comes from the env var.
export TESSERA_PASSPHRASE='smoke-test-passphrase'

# 4. Initialise
tessera init                       # creates ~/.tessera/vault.db

# 5. Register the embedding model (daemon refuses to start without one).
#    First call after activation downloads ~520 MB of ONNX weights to
#    ~/.cache/fastembed; pick the -Q quantised variant if the link is slow.
tessera models set --name nomic-ai/nomic-embed-text-v1.5 --dim 768 --activate

# 6. Start daemon and verify health.
#    First start triggers fastembed embedder + reranker weight downloads.
tessera daemon start
tessera doctor                     # all green

# 7. Wire one client
tessera connect claude-desktop --token-ttl-days 30

# 8. Capture and recall (CLI form, since Claude Desktop install is platform-variable)
tessera capture "I prefer uv over pip for Python." --facet-type preference
tessera capture "anneal — Artifact-Eval-Agent triplet, git worktrees for isolation." --facet-type project
tessera capture "$(printf 'Hook → Legend → Credibility Spike → Observation → Meaning. 150–300 words. No emojis.')" --facet-type workflow
tessera recall "LinkedIn post about anneal"

# 9. Stop daemon, capture timing
tessera daemon stop
```

**Pass criteria:**

- Every command exits 0.
- `tessera doctor` reports no red checks. `fastembed: ok` after step 6.
- `tessera recall` returns at least one facet from each captured type.
- Total wall time from step 2 to step 8 (excluding the one-time fastembed download in step 5/6): under 10 minutes.

**Capture for the recording:**

- Terminal output of every step (asciinema or platform equivalent).
- `tessera doctor` output.
- `tessera stats` output before stopping the daemon.
- One screenshot of the host OS confirming clean-VM identity.

---

## Failure-mode triage

| Symptom | Likely cause | Mitigation |
|---|---|---|
| `pip install` fails on `sqlcipher3` build | Missing `libsqlcipher-dev` or `brew install sqlcipher` | Install per `docs/troubleshooting.md §sqlcipher3`; on Windows fall back to WSL2 |
| `tessera daemon start` hangs at first run | fastembed weight download stalled | Check `~/.cache/fastembed` for partial files; rerun once network stabilises (downloads resume) |
| `tessera daemon start` fails with `NoActiveModelError` | Skipped step 5 | Run `tessera models set --activate` per quickstart §5 |
| `tessera doctor` flags `fastembed: cache not present` | Cache directory hasn't been populated yet | Harmless before first daemon start; run `tessera models test --name <fastembed-id>` to warm it out-of-band |
| `tessera doctor` flags `fastembed: import failed` | Broken Python install / missing onnxruntime wheel | `pipx install --pre tessera-context==0.4.0rc1 --force` re-resolves the wheel set |
| `recall` returns no matches | Embed worker still warming or the captured facet hasn't been embedded yet | Wait ~5 s for the embed worker idle pass, retry; check `tessera stats` for `pending` count |

---

## Gate-closure criteria

The runbook is closed when:

1. Three recorded clean-install walkthroughs (one per platform) land under `docs/benchmarks/` or a sibling artifact directory, each showing every command exit 0 and every doctor check OK.
2. Failure-mode entries that surface during recordings are added to `docs/troubleshooting.md`.
3. `docs/release-spec.md §Definition of Done for v0.4` flips the cross-platform smoke checkbox.

The decision to publish v0.4.0rc1 to PyPI does NOT depend on this runbook closing — same model as v0.1.0rc1 and v0.3.0rc1, where rc publication ran ahead of the recordings on the strength of CI evidence.
