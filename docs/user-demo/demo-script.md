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
| 1. Claude Desktop connect | 1:00–1:30 | `tessera connect claude-desktop`, restart Claude, verify MCP tool surface |
| 2. Capture four facets in Claude | 1:30–5:30 | Claude conversation: four `capture` calls |
| 3. Verify vault state | 5:30–6:00 | terminal: `tessera stats`, `tessera list_facets` |
| 4. ChatGPT connect | 6:00–7:00 | ChatGPT Dev Mode MCP config |
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
# Copy the printed raw token; you'll paste it into Claude Desktop's config.

# 4. Start the daemon.
tessera daemon start --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
tessera daemon status
# Expect: vault_id=<ulid>  active_model_id=1  schema_version=2
```

**Pass gate:** `tessera daemon status` returns a ULID and model 1. If `vault_id` is empty, the daemon didn't warm up — check the log at `~/.tessera/run/tesserad.log`.

## Stage 1 — Claude Desktop (30 sec)

```bash
tessera connect claude-desktop --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
```

On-camera action: open **Claude Desktop**, Cmd-Q, re-open. The MCP tool menu should show `capture`, `recall`, `show`, `list_facets`, `stats`, `forget` as available tools under "Tessera".

**Pass gate:** six tools visible in Claude's MCP panel. If only the HTTP URL appears without tools, the token was mis-pasted — re-run `tessera connect` and restart Claude.

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

**Pass gate:** Claude confirms four facets captured. Optionally: `tessera stats --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"` prints `facets=4, by_type={preference=1, workflow=1, project=1, style=1}`.

## Stage 3 — Verify vault state (30 sec)

```bash
tessera stats --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
tessera list_facets --vault ~/.tessera/demo.db --passphrase "$TESSERA_PASSPHRASE"
```

**Pass gate:** all four facets listed with their external-IDs. This shot is the evidence-on-camera that the capture worked.

## Stage 4 — ChatGPT connect (1 min)

Open ChatGPT Developer Mode. In the MCP server configuration:

- URL: `http://127.0.0.1:5710/mcp?token=<RAW_TOKEN>` (use the token from Stage 0).
- Server name: `tessera`.

On-camera action: paste URL, confirm the six tools appear in ChatGPT's tool list.

**Pass gate:** ChatGPT shows `recall`, `capture`, etc. under the `tessera` server.

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
| `recall` returns empty result set | No facets captured yet, or active model mismatch | `tessera list_facets` on camera — if empty, loop back to Stage 2 |
| ChatGPT MCP panel says "can't reach server" | Token TTL expired | `tessera tokens create` again; update the ChatGPT URL |
| p99 spike > 3 s on a `recall` | Ollama cold-reload | Wait 30 s and re-run — `keep_alive=-1` should prevent repeat |

If any row fires, **stop recording and reset**. Do not ship a video with a recovery path visible — the real-user test then can't distinguish product issues from recording issues.

## Teardown after recording

```bash
tessera daemon stop || true
rm ~/.tessera/demo.db ~/.tessera/demo.db.salt
unset TESSERA_PASSPHRASE
```
