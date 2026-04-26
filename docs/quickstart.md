# Tessera — Pitch & Quick-Start

> _Teach it once. Use it anywhere._
> Portable context that travels with you across every AI tool.

Apache 2.0. Local-first. Single SQLite file. No telemetry. No hosted service. No account.

---

## Why this exists

Every AI tool you use is an amnesiac you re-onboard from scratch. CLAUDE.md for Claude. Custom Instructions for ChatGPT. Cursor Rules for Cursor. Codex config for Codex. You teach each one your preferences, your workflows, your projects, your writing voice — and then you do it again in six weeks when the next model ships.

There is no layer between you and the models. The moment you switch tools, you start over. Tessera is that layer.

A local daemon owns a single SQLite file at `~/.tessera/vault.db`. The file holds your _context_ across five facets: who you are (`identity`), how you work (`preference`), procedures you follow (`workflow`), what you're building (`project`), and how you write (`style`). Any MCP-capable AI tool reads and writes that context with a scoped capability token. You teach Claude once that you prefer `uv` over `pip` and Cursor inherits it. You paste three LinkedIn posts as voice samples in Claude Desktop and ChatGPT drafts your fourth post in your voice without you ever opening ChatGPT's settings.

## Who it's for

The T-shaped AI-native operator. Deep in one or two domains; horizontal across many through AI tools. If you've written a `CLAUDE.md` and wished it worked everywhere, you are the user.

## How it's different

| Class                                                                                                     | What it does                            | Why Tessera is different                                                                                                                                        |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Per-tool preference files (CLAUDE.md, ChatGPT Custom Instructions, Cursor Rules, Codex config)            | One file per tool                       | Cross-tool by design; one source of truth                                                                                                                       |
| Cloud-hosted memory layers (Mem0, OpenMemory, Cognee, MemPalace, the cloud-Postgres "second brain" class) | Memory layer in someone else's database | Your vault is a single file on your disk; structured context (preferences, workflows, projects, style), not flat blobs; cross-facet coherent retrieval via SWCR |

## What's real today

`v0.4.0rc2` lives on PyPI as `tessera-context`. Five facets plus the skill surface, eleven MCP tools (`capture`, `recall`, `show`, `list_facets`, `stats`, `forget`, `learn_skill`, `get_skill`, `list_skills`, `resolve_person`, `list_people`), a parallel REST surface at `/api/v1/*` for hooks and shell scripts, all-local by absence (fastembed via ONNX Runtime in-process for both embedding and reranking — no Ollama, no torch, no cloud adapters), encrypted vault (sqlcipher + argon2id), ChatGPT + Claude conversation-history importers, named skills synced to disk, people resolution, zero outbound network unless you explicitly opt in.

The remaining gates before v0.4.0 GA are a recorded clean-VM walkthrough on macOS / Ubuntu / Windows. Runbook: [`docs/smoke-test-v0.4rc1.md`](smoke-test-v0.4rc1.md). DoD: [`docs/release-spec.md`](release-spec.md).

---

## Quick-start (≈10 minutes)

### 1. Prerequisites

```bash
# Python 3.12 (3.13 not yet supported — sqlcipher3 wheel gap)
python3 --version

# macOS — sqlcipher headers
brew install sqlcipher

# Ubuntu / Debian — build deps for sqlcipher3 wheel
sudo apt install -y build-essential libsqlcipher-dev

# Windows — sqlcipher3 has no native wheel today; WSL2 + Ubuntu is the
# supported path until that ships. Install Python 3.12 inside WSL2.
```

Embedding and reranking run in-process via fastembed (ONNX Runtime). The first daemon start downloads model weights to `~/.cache/fastembed`; no separate model server, no Ollama, no torch.

### 2. Install

```bash
# pipx is the friendliest path — isolated venv, console script on PATH
pipx install --pip-args="--pre" tessera-context==0.4.0rc2

# or plain pip (note: --pre is required while we're on rc)
pip install --pre tessera-context==0.4.0rc2

tessera --version          # expect 0.4.0rc2
```

For development against the latest branch (in case you want unreleased fixes between rcs), source-install instead:

```bash
git clone https://github.com/Mathews-Tom/Tessera.git
cd Tessera && uv sync --dev
uv tool install -e . --force
tessera --version
```

### 3. Setup once

Tessera resolves the vault path and passphrase from environment variables so day-to-day commands run without flags. The defaults match the v0.1 single-vault solo-developer case.

```bash
# pick a passphrase, store it in your shell so every command picks it up.
# add to ~/.zshrc / ~/.bashrc / global .env — anywhere your interactive
# shells already source.
export TESSERA_PASSPHRASE='your-passphrase-here'

# the vault path defaults to ~/.tessera/vault.db. Override only if you keep
# vaults elsewhere or run more than one (e.g. work.db + personal.db).
# export TESSERA_VAULT="$HOME/Vaults/tessera/work.db"
```

Resolution order, applied per command: explicit flag → env var → default. Pass
`--vault` / `--passphrase` for one-off runs without exporting; the env-var
version is what makes the daily flow flag-free.

### 4. Initialise the vault

```bash
tessera init                      # creates ~/.tessera/vault.db (or $TESSERA_VAULT)
```

### 5. Register an embedding model

`tessera init` bootstraps the vault but does not register an embedder; the daemon refuses to start without one. Pick a fastembed model and flag it active:

```bash
tessera models set \
  --name nomic-ai/nomic-embed-text-v1.5 \
  --dim 768 \
  --activate
```

`--name` is the fastembed model identifier — anything from `TextEmbedding.list_supported_models()` works. `--dim` must match the model's declared embedding dimensionality (768 for `nomic-embed-text-v1.5`). `--activate` flips this model's `is_active` row to true so the embed worker and retrieval pipeline pick it up. You can register additional models without `--activate` and switch the active model later with another `tessera models set --activate` call. The first call after activation downloads the model weights (~520 MB for the default; ~130 MB for the `-Q` quantised variant if you want a smaller footprint) to `~/.cache/fastembed`.

### 6. Start the daemon

```bash
tessera daemon start              # starts tesserad
tessera doctor                    # all green = ready
```

### 7. Wire your AI tools

```bash
# One shot — every detected file-based MCP client
tessera connect all --token-ttl-days 30

# Or per-tool, with control over scope
tessera connect claude-desktop
tessera connect claude-code
tessera connect cursor
tessera connect codex
```

Default service-token TTL is 24 hours. `--token-ttl-days 30` is the "set and forget" personal-use mode (cap 90).

ChatGPT Developer Mode is deferred to v0.1.x — three stacked blockers (HTTPS front, Bearer auth in the New App dialog, canonical HTTP MCP). Two Anthropic clients sharing one vault still demonstrates the portability story today.

### 8. Teach it the first thing

In Claude Desktop or Claude Code, with the Tessera MCP wired:

```text
You: Capture a preference: I prefer `uv` over `pip` for Python. Never suggest `pip install`.
Claude: [calls capture(content, facet_type="preference")]

You: Capture this LinkedIn workflow: 5-act structure — Hook → Legend → Credibility Spike → Observation → Meaning. 150–300 words. No emojis.
Claude: [calls capture(content, facet_type="workflow")]

You: Capture the active project context: anneal — Artifact-Eval-Agent triplet, git worktrees for isolation, Apache 2.0.
Claude: [calls capture(content, facet_type="project")]
```

Now open a different MCP-capable tool (Claude Code, Cursor, Codex) and ask it to draft a LinkedIn post about your project. It will call `recall("LinkedIn post anneal")` and the SWCR-weighted bundle returns your style, your workflow, your project, your no-emoji preference — together — without you configuring the second tool.

---

## Daily personal use

The five facets are the unit of capture. Use them deliberately:

| Facet        | What goes here                                                                            | Capture cadence                                              |
| ------------ | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `identity`   | Stable-for-years user facts (role, location, professional context)                        | Once, then revisit yearly                                    |
| `preference` | Stable-for-months behavioural rules (`uv` over `pip`, async-first, terse Reddit register) | Whenever a tool produces something you'd correct             |
| `workflow`   | Procedural patterns (5-act LinkedIn, weekly review structure, PR description template)    | Whenever you catch yourself re-explaining a procedure        |
| `project`    | Active work context (anneal architecture, current quarter goals)                          | Weekly; soft-delete via `tessera forget` when projects close |
| `style`      | Writing voice samples (3-5 representative posts per channel)                              | Once per output channel, refresh quarterly                   |

### Capture from the CLI

CLI commands that talk to the daemon (`capture`, `recall`, `show`, `stats`, `forget`) authenticate with a bearer token, the same way an MCP-wired client does. Mint a long-lived service token once and put it in your shell:

```bash
tessera tokens create \
  --client-name cli \
  --token-class service \
  --read '*' --write '*' \
  --token-ttl-days 30
# copy the printed token (shown once) into your shell rc:
export TESSERA_TOKEN="paste-the-token-here"
```

Then capture and recall feel native:

```bash
tessera capture "I write Reddit comments terse, slightly abrasive, in-group aware. 4 sentence max." --facet-type preference
tessera capture "$(cat my-best-linkedin-post.md)" --facet-type style
```

### Recall and inspect

```bash
tessera recall "LinkedIn post about anneal"          # cross-facet by default
tessera recall "Python deps" --facet-types preference
tessera show <external_id>                            # full row
tessera stats                                         # facet counts per type
```

### Soft-delete (audit-logged, reversible at SQL layer)

```bash
tessera forget <external_id> --reason "project closed 2026-Q1"
```

---

## v0.3 features (on the active branch)

### Skills — named procedures, synced to disk

```bash
# Author through any MCP client via learn_skill, then materialise to disk:
tessera skills sync-to-disk /path/to/your/skills-dir

# Edit a .md file in your editor, then reconcile back:
tessera skills sync-from-disk /path/to/your/skills-dir

# Inspect:
tessera skills list
tessera skills show "git-rebase-cleanup"
```

Pick any directory you control — common choices are a folder inside an existing dotfiles repo or a dedicated `~/notes/skills/` path. Pair it with a Git repo to version-control your skill library independently of the vault.

### People — resolution, not extraction

People are stored as rows in a `people` table (not facets) with canonical name + alias array. Auto-extraction from imported conversations is **not** shipped — it would create silent false-positive person rows you can't easily undo. Use `tessera people` and the `resolve_person` MCP tool to surface candidates and let your AI client ask you when ambiguous.

### Importers — backfill from your existing AI history

```bash
# Claude data export: Settings → Privacy → Export Data → conversations.json
tessera import claude --file ~/Downloads/claude-export/conversations.json

# ChatGPT data export: Settings → Data Controls → Export → conversations.json
tessera import chatgpt --file ~/Downloads/chatgpt-export/conversations.json
```

Importers write `project` facets only — never `skill` or `person`. Skills stay user-authored; people surface through interactive resolution. This keeps your skill library and contact graph free of silent NER false positives.

---

## Maintenance & portability

```bash
tessera doctor                                # health check, exit non-zero on red
tessera doctor --collect bundle               # scrubbed .tar.gz for issue reports

tessera export --format json   --output ~/Backups/tessera-$(date +%F).json
tessera export --format md     --output ~/Backups/tessera-md/
tessera export --format sqlite --output ~/Backups/vault-decrypted.db

# Reset embed status for facets that errored or for one facet type after a model swap:
tessera vault repair-embeds
tessera vault repair-embeds --facet-type style

# Or just copy the file. The vault IS the product.
cp ~/.tessera/vault.db ~/Backups/vault-$(date +%F).db
```

### Token hygiene

```bash
tessera tokens list
tessera tokens revoke --token-id <id> --reason "rotated"
tessera disconnect cursor                     # remove tool's MCP entry without stomping siblings
```

---

## What it explicitly will not become

No telemetry. No auto-capture (you decide what gets stored). No AI-generated capture without explicit user intent. No hosted-only mode in v0.1. No model reselling, ever. No paid features in the open-source core, ever. Full list at `docs/non-goals.md`.

If a real audience forms, the long-term shape is optional managed sync (BYO storage always free) — Obsidian Sync's playbook. That is years out and not the reason this exists.

---

## Where to read next

- [`docs/pitch.md`](pitch.md) — the deeper colleague-pitch with framing, market context, and what to push back on.
- [`docs/system-overview.md`](system-overview.md) — category claim, moat analysis, T-shape framing.
- [`docs/system-design.md`](system-design.md) — architecture, schema, retrieval pipeline, encryption.
- [`docs/swcr-spec.md`](swcr-spec.md) — the cross-facet retrieval algorithm.
- [`docs/release-spec.md`](release-spec.md) — what ships in v0.1 / v0.3 / v0.5 / v1.0.
- [`docs/troubleshooting.md`](troubleshooting.md) — symptom-indexed install and first-run fixes.

---

## What I'd ask of you if you try it

1. Does the framing land? "Portable context layer for every AI tool" — does it feel like a category, or a memory product with a paint job?
2. Where does the demo break in your head? When you imagine teaching one tool and recalling in another, what's the part you don't believe?
3. Who would actually install this in your network? Not "who would think it's interesting" — who would change their setup. T-shaped engineers, people running 3+ AI tools, people who've written a CLAUDE.md and wished it worked everywhere.

Issues, fixes, and direct feedback: <https://github.com/Mathews-Tom/Tessera>.
