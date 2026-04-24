# T-Shape Demo Script — 10-Minute Walkthrough

This is the command-by-command script the recorder follows with a stopwatch. Every step has an expected output and a pass gate; the script is designed so a pause at any point means the previous step failed and the recording should stop and restart clean.

## Pre-flight (do this before the camera is rolling)

```bash
scripts/demo_smoke.sh
```

Stop if red. The rest of the script assumes green.

Close every app that writes to `~/.tessera/` except the terminal you'll record. Check that the microphone is unmuted; check that screen-recorder is capturing audio + video.

## The 60-second briefing (read on-camera first)

> "Tessera is a portable context layer for T-shaped AI-native users. I'm going to capture four facets in Claude — my preferences, a workflow, a project note, a style sample — then open ChatGPT and have it draft a LinkedIn post using those captured facets as the context. The vault is on-disk, sqlcipher-encrypted, talks to both clients over MCP. All-local: no cloud keys, no telemetry. The whole thing should take under 10 minutes."

## Time budget

| Stage | Wall-clock | What the camera sees |
|-------|-----------:|----------------------|
| 0. Bootstrap | 0:00–1:00 | terminal: `tessera init`, model registered, daemon started |
| 1. Connect capture-side client | 1:00–1:30 | `tessera connect <client>` (any of claude-desktop, claude-code, cursor, codex), restart, verify MCP tool surface |
| 2. Capture four facets in the client | 1:30–5:30 | Client conversation: four `capture` calls |
| 3. Verify vault state | 5:30–6:00 | Client invokes the `list_facets` MCP tool, displays the four captured facets |
| 4. ChatGPT connect | 6:00–7:00 | ChatGPT → Developer Mode → New App: Name / MCP Server URL / Authentication=Bearer / Bearer token |
| 5. Cross-facet recall + draft | 7:00–9:30 | ChatGPT: `recall(facet_types=all)`, then a draft request |
| 6. Close and verify | 9:30–10:00 | terminal: `tessera doctor`, `tessera daemon stop` |

## Stage 0 — Bootstrap (1 min)

```bash
# 1. Initialise an encrypted vault at the default location.
export TESSERA_PASSPHRASE="demo-$(date +%s)"
tessera init --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"

# 2. Register the embedding model. nomic-embed-text is pulled already per the pre-flight.
tessera models set \
    --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE" \
    --name ollama --model nomic-embed-text --dim 768 --activate

# 3. Create a capability token scoped to the five v0.1 facet types.
tessera tokens create \
    --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE" \
    --client-name demo --token-class session \
    --read-scope identity,preference,workflow,project,style \
    --write-scope identity,preference,workflow,project,style
# --agent-id is auto-selected because `tessera init` created exactly
# one default agent. If the vault has >1 agents, add `--agent-id N`.
# Copy the printed access_token value; you'll paste it into Claude's config.

# 4. Start the daemon. `daemon start` detaches tesserad as a background
# process and shows an infinite spinner ("⠋ waiting for daemon to be
# ready") until the control socket answers. Returns with a panel of
# pid / vault_id / active_model_id / log path. For a persistent daemon
# that survives reboot, `tessera daemon install` writes the launchd
# plist / systemd user unit — not used in the recording.
tessera daemon start --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
# Re-query status any time (the panel shows the live values):
tessera daemon status
# Expect: vault_id=<ulid>  active_model_id=1  schema_version=2
```

**Pass gate:** `tessera daemon status` returns a ULID and model 1. If `vault_id` is empty, the daemon didn't warm up — check the log at `~/.tessera/run/tesserad.log`.

## Stage 1 — Connect the capture-side client (30 sec)

The recording uses **Claude Desktop** on the capture side, but any of the four file-based v0.1 connectors work: `claude-desktop`, `claude-code`, `cursor`, `codex`. Pick whichever matches your daily-driver AI tool. Pass multiple ids in one command to connect several at once, or use `all` as sugar for every file-based client (ChatGPT is separate because it uses the Developer Mode "New App" paste flow covered in Stage 4):

```bash
# Connect only the client you'll capture from on camera.
tessera connect claude-desktop --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"

# Equivalent for Claude Code users:
# tessera connect claude-code --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"

# Or connect every file-based client at once (what a thorough demo does):
# tessera connect all --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
```

On-camera action: open the chosen client (for Claude Desktop that's Cmd-Q + reopen; Claude Code restarts the next time you start the CLI; Cursor + Codex pick up the MCP server on next launch). The MCP tool menu should show `capture`, `recall`, `show`, `list_facets`, `stats`, `forget` as available tools under "Tessera".

**Pass gate:** six tools visible in the client's MCP panel. If only the HTTP URL appears without tools, the token was mis-pasted or the daemon is not running — re-run `tessera connect <client>` after verifying `tessera daemon status` returns a vault_id.

## Stage 2 — Capture four facets in Claude (4 min)

Read each prompt verbatim to Claude. The prompts are optimised to produce a natural `capture` tool call.

**Preference** (1 min):
> "Please capture this preference in Tessera: I prefer terse, evidence-backed technical writing with no hedging and no emoji. I default to imperative mood in instructions."

**Workflow** (1 min):
> "Capture this workflow: when I review a pull request, I read the PR description first, then the test diff, then the source diff, then the commit messages — in that order. I reject PRs with green CI but no test coverage of new branches."

**Project** (1 min):
> "Capture this project fact: I'm working on a local-first memory layer called Tessera. Current release is v0.1, shipping shortly. The core invariant is all-local by default, zero telemetry, sqlcipher-encrypted vault, capability-scoped per-tool access."

**Style** (1 min):
> "Capture a style sample. I write LinkedIn posts in this voice: 'The measured p50 latency at 10K facets was 730ms. The pre-measurement target was 500ms. The gap is structural: Ollama embed, sqlite-vec linear scan, SWCR, MMR. None are cheap; each is the right call for the constraint. Shipping v0.1 with the measured envelope documented, revising in v0.1.x.'"

**Pass gate:** Claude confirms four facets captured. Optionally verify on camera by asking the client to call the `stats` MCP tool; it returns `{"facets": 4, "by_facet_type": {"preference": 1, "workflow": 1, "project": 1, "style": 1}}`.

## Stage 3 — Verify vault state (30 sec)

On-camera action: ask the connected client to call the `list_facets` MCP tool (Claude / Cursor / Codex all expose it through the Tessera MCP server). Read to the client:

> "Please call the `list_facets` tool on Tessera and show me all four facets you captured, with their external_ids and facet types."

The tool returns a structured list of the four captured facets. This is the evidence-on-camera that the capture worked — no terminal shot is needed because the client's MCP panel already renders the tool call and its response.

**Terminal-side alternative** (for recording with a split-screen terminal): `tessera stats` and `tessera show <external_id>` are MCP passthroughs that need a bearer token. Export the access token from Stage 0 first (`export TESSERA_TOKEN=<raw_token>`) then call `tessera stats`.

**Pass gate:** all four facets listed with their external-IDs (via the MCP tool output or the terminal variant).

## Stage 4 — ChatGPT Developer Mode connect (1 min)

Follow [OpenAI's Developer Mode guide](https://developers.openai.com/api/docs/guides/developer-mode). The "New App (Beta)" dialog takes four fields; `tessera connect chatgpt` prints exactly the values to paste into each.

```bash
tessera connect chatgpt --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
# prints a kv-panel with:
#   Name            Tessera
#   MCP Server URL  http://127.0.0.1:5710/mcp
#   Authentication  Bearer
#   Bearer token    tessera_service_...
```

On-camera action:

1. ChatGPT → **Settings** → **Developer Mode** → **New App**.
2. Paste the four values from the panel into their matching fields. `Name` is free-text; `MCP Server URL` and `Bearer token` come verbatim; `Authentication` dropdown → select `Bearer`.
3. Tick "**I understand and want to continue**" (OpenAI's safety warning is boilerplate for custom MCP servers).
4. Click **Create**.

**Pass gate:** ChatGPT shows `capture`, `recall`, `show`, `list_facets`, `stats`, `forget` under the `Tessera` app in the tool-picker sidebar.

**⚠ Known integration gap to watch for.** ChatGPT's MCP client speaks canonical MCP JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`). Tessera's `/mcp` endpoint currently speaks a custom `{"method": X, "args": Y}` shape — the same protocol gap that hit Claude Desktop and that `tessera stdio` solves for the stdio side. If ChatGPT reports a connection error or the tools do not appear, the HTTP-side bridge is the v0.1.x follow-up that closes this. Flag the gap in the recording notes rather than ship a broken take.

## Stage 5 — Cross-facet recall + draft (2.5 min)

Read to ChatGPT:

> "Call the Tessera `recall` tool with `facet_types=all` and the query 'LinkedIn post about v0.1 shipping with measured latency numbers'. Then draft a LinkedIn post for me using what you got back."

Watch the tool-call panel:
- `recall` returns a bundle with at least one facet from each of `preference`, `workflow`, `project`, `style`.
- ChatGPT's draft should reflect the **preference** (terse, no hedging, no emoji), the **project** (v0.1 shipping), and the **style** (numeric-first, structural diagnosis).

**Pass gate:** a single LinkedIn post ChatGPT wrote that a reader could mistake for Tom's own voice. This is the moment the demo is trying to produce. If the draft is generic or hedge-heavy, SWCR lost the coherence bundle — stop and capture the observed bundle for debugging.

## Stage 6 — Close and verify (30 sec)

```bash
tessera doctor --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
tessera daemon stop
```

**Pass gate:** doctor reports all OK or a single WARN for keyring. Daemon stop returns clean.

## Recovery paths (if something breaks on camera)

| Symptom | Likely cause | Recovery |
|---------|--------------|----------|
| `daemon status` shows empty `vault_id` | Warm-up failed | `tail ~/.tessera/run/tesserad.log`; common: missing nomic-embed-text → `ollama pull nomic-embed-text` |
| Claude doesn't see Tessera tools | Claude cached old config | `tessera disconnect claude-desktop`, then `tessera connect claude-desktop`, full restart |
| `recall` returns empty result set | No facets captured yet, or active model mismatch | Ask the client to call the `list_facets` MCP tool — if empty, loop back to Stage 2. (There is no `tessera list_facets` CLI subcommand; the verb only exists as an MCP tool.) |
| ChatGPT MCP panel says "can't reach server" | Token TTL expired, or protocol gap (ChatGPT speaks canonical MCP JSON-RPC 2.0; Tessera's `/mcp` speaks custom shape) | Re-run `tessera connect chatgpt` to mint a fresh token. If the fresh token still fails, the server-side HTTP bridge is a v0.1.x follow-up — flag and continue to terminal-side verification instead. |
| p99 spike > 3 s on a `recall` | Ollama cold-reload | Wait 30 s and re-run — `keep_alive=-1` should prevent repeat |

If any row fires, **stop recording and reset**. Do not ship a video with a recovery path visible — the real-user test then can't distinguish product issues from recording issues.

## Teardown after recording (or before re-recording)

The documented way to reset every artifact the demo creates — vault, salt sidecar, daemon runtime (pid / log / socket / events.db), and the per-client MCP config entries — is a single script:

```bash
scripts/demo_reset.sh
# or, against a non-default vault path:
scripts/demo_reset.sh ~/.tessera/other.db
```

The script is idempotent: running it twice is safe, running it against state where some artifacts are already absent is safe. It stops the daemon (if running), removes vault + salt + runtime files, and calls `tessera disconnect` for `claude-desktop`, `claude-code`, `cursor`, and `codex`. It leaves Ollama untouched (model stays pulled) and does not uninstall a persistent launchd/systemd unit (use `tessera daemon uninstall` separately).

Afterwards, clear the shell env var yourself (the script cannot modify the parent shell):

```bash
unset TESSERA_PASSPHRASE TESSERA_TOKEN
```
