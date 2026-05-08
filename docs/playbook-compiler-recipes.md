# Playbook compiler recipes

**Status:** Stable for V0.5.
**Audience:** Users compiling AgenticOS Playbooks against a Tessera vault.
**Boundary:** Tessera stores; the caller compiles. ADR 0019 §Boundary statement keeps LLM execution outside the daemon. Every recipe in this document drives an external runner around the `tessera playbook` CLI.

## Why a recipe pack

The compiler-orchestration CLI (`docs/api.md §CLI: tessera playbook`) ships five subcommands plus the read-side `inspect`: `targets`, `sources`, `scaffold`, `register`, `stale`, and `inspect`. None of them call an LLM. The deterministic Markdown brief that `scaffold` writes is the sole authoring surface — every runner reads the same brief, compiles externally, and then registers the result through the same `register` command.

A recipe is a documented, reproducible loop around that contract. It names the runner, the compiler-version string the runner stamps onto the artifact, the eval workflow, and the staleness recompile loop. This document defines the three runners V0.5 supports and the conventions they share.

## Shared contract for every recipe

Every recipe follows the same skeleton:

1. **Confirm the target descriptor exists** with `tessera playbook targets`. A Playbook without a registered descriptor (`target`, `task`, `artifact_type`, `quality_bar`) is not a recipe-eligible compile.
2. **Enumerate sources** with `tessera playbook sources <target>`. The runner uses this list as the source-of-truth source set for the compile.
3. **Generate the brief** with `tessera playbook scaffold <target> --out <path>`. The brief is deterministic: target, task, source-facet table, required output sections, eval-set guidance, and provenance expectations are all written by Tessera, not by the runner.
4. **Compile externally** using the runner's chosen authoring surface (Claude Code, a local LLM, or a human editor). The runner reads the brief, reads the source facets through `tessera curl show <ulid>` (or `GET /api/v1/facets/<ulid>` via the daemon), and writes a Markdown artifact body that satisfies the brief's required output sections. `tessera playbook inspect` is the read surface for compiled artifacts only; source-facet bodies travel through the facet read path instead.
5. **Run the eval set** for the target. Tessera does not execute evals. The runner is responsible for running representative questions, scoring `expected_claims`, checking `required_source_refs`, and recording the pass/fail summary in the registered artifact's body under the `## Eval summary` section. Failed eval detail belongs verbatim in that section so future readers can audit the run.
6. **Register the compiled artifact** with `tessera playbook register <target> --content <path> --compiler-version <version>`. The runner picks an explicit source set with `--source-id <ulid>` only when it diverges from the `compile_into` enumeration; the default is the full enumeration.
7. **Resolve staleness on the next run** with `tessera playbook stale --json` and recompile any artifact whose source mutations broke the answer. ADR 0019 §pair-write keeps the artifact inspectable while stale; V0.5-P7 §Playbook retrieval and staleness contract requires stale matches to surface as `is_stale=true` until the recipe replaces them.

The CLI never accepts caller metadata at register time today. `eval_summary` and `field_provenance` therefore live inside the artifact Markdown body for every recipe in this pack. Callers that need machine-readable metadata on the registered artifact use the Python API (`tessera.vault.compiled.register_compiled_artifact(..., metadata=...)`) directly; the recipe pack flags those advanced flows but does not require them for the V0.5 dogfood gate.

## Compiler version naming

Every `register` call carries a `--compiler-version` string. The string is stamped onto `compiled_artifacts.compiler_version` and surfaced in `tessera playbook inspect`, `tessera playbook stale`, and the audit log. Recipes share one convention so the version can be read without reverse-engineering the runner.

```text
<runner-name>/<recipe-name>@<semver-or-date>
```

| Field | Meaning |
| --- | --- |
| `runner-name` | The compiler runtime: `claude-code`, `ollama`, `manual`, etc. Lowercase ASCII; hyphens allowed. |
| `recipe-name` | The recipe inside this pack the runner used: `release-recipe`, `swcr-recipe`, `manual`, etc. |
| `semver-or-date` | Version of the recipe, the prompt, or the runner. Use `YYYY-MM-DD` when prompts are date-stamped; use semver when the runner ships versioned releases. |

Examples:

```text
claude-code/release-recipe@2026-05-08
ollama/llama3.1-70b-recipe@2026-05-09
manual/release-recipe@1.0.0
```

The string is required and case-sensitive. The CLI rejects empty values and limits the length to 128 characters.

## Minimum artifact sections

Every artifact body produced by a recipe in this pack carries the seven sections listed in `.docs/compiled-playbooks-enhancement-plan.md §Phase 8` and `tessera playbook scaffold`'s required output sections list. The scaffold writes them as a numbered list inside the brief. Compiled artifacts repeat them as Markdown headings so `tessera playbook inspect --field` can slice each one.

| Section | Heading | Purpose |
| --- | --- | --- |
| Purpose | `## Purpose` | What recurring task the artifact accelerates and why a Playbook is the right shape. One paragraph; cites the `task` and `quality_bar` from the descriptor. |
| Supported tasks | `## Supported tasks` | Concrete recurring questions the artifact must answer well. Mirrors the eval-set `question` list when one exists. |
| Source inventory | `## Source inventory` | Every facet ULID the compile read, paired with `compile_role` when set. Cross-checks against `compiled_artifacts.source_facets`. |
| Synthesized operating model | `## Synthesized operating model` | The Playbook content itself: the task-shaped synthesis the recurring task needs. The bulk of the artifact lives here. |
| Known gaps | `## Known gaps` | What the sources do not cover. Honest limits beat invented detail; this section is non-empty even when the answer is "no known gaps in scope today." |
| Eval summary | `## Eval summary` | The eval-set pass/fail counts (`passed`, `failed`, `skipped`), the runner's `compiler-version`, and `must` failure detail verbatim. Recipes that skip evals state that explicitly. |
| Provenance notes | `## Provenance notes` | Claim-to-source backing for the highest-stakes statements. Reuses the source ULIDs from the inventory; optional `field_provenance` metadata reuses the same ULIDs. |

`tessera playbook inspect <target> --field "Eval summary"` and `--field "Provenance notes"` work against a heading-by-heading match (V0.5-P7). Recipes therefore use those exact heading strings; reword them only when the recipe explicitly carries a renamed `field_provenance` key for the section.

## Recipe 1 — Claude Code

**Runner:** Claude Code, run interactively or through `claude -p` headless mode.
**Compiler-version stub:** `claude-code/<recipe-name>@YYYY-MM-DD`.

This recipe is the fastest loop. Claude Code already runs inside the user's worktree, can read the brief and source facets through `tessera playbook` directly, and can write the artifact body in one session. The recipe is "scaffold once, compile in a session, register once."

### Steps

1. **Pick the target and worktree.** Decide which target to compile. Open a worktree that has read access to the vault and `tessera` on the path.
2. **Generate the brief.**

   ```bash
   tessera playbook scaffold release_playbook --out .scratch/release.brief.md
   ```

3. **Open the brief in a Claude Code session.** Hand the brief to Claude Code as the task prompt. The brief carries every constraint the runner needs: target, task, source-facet ULIDs, required sections, eval-set guidance, provenance expectations.
4. **Read each source facet.** Claude Code calls `tessera curl show <ulid>` (or hits `GET /api/v1/facets/<ulid>` directly when a daemon is running) for each ULID in the brief's source-facet table. `tessera playbook inspect <ulid>` is the read surface for compiled artifacts, not source facets — using it on a source ULID returns "no compiled artifact with external_id" and is a recipe bug, not a daemon bug.
5. **Compile the artifact body.** Claude Code drafts the seven required sections in a Markdown file alongside the brief. The model name running Claude Code is not the compiler version; the recipe name is. Stamp `claude-code/release-recipe@2026-05-08` (or the active recipe + date) onto the artifact when the file is final.
6. **Run the eval set.** For each eval entry, ask Claude Code to answer the question in a fresh sub-session against the artifact body, then score the answer's `expected_claims` and check `required_source_refs`. Record the pass/fail counts and any `must` failure verbatim under `## Eval summary` in the artifact body.
7. **Register the artifact.**

   ```bash
   tessera playbook register release_playbook \
       --content .scratch/release.playbook.md \
       --compiler-version "claude-code/release-recipe@2026-05-08"
   ```

8. **Recompile when stale.** On the next dogfood loop, run `tessera playbook stale --json`. For each stale `external_id` whose target this recipe owns, repeat steps 2–7 against fresh sources.

### Variant: headless `claude -p`

Replace step 3 with `claude -p "$(cat .scratch/release.brief.md)" > .scratch/release.playbook.md`. Steps 4–7 stay identical. The headless variant trades interactive review for reproducible scripted compiles; it is the right shape for scheduled recompiles or CI-driven dogfood, not for the first compile of a new target.

### Boundary reminders

- Do not paste vault contents into a third-party hosted Claude session. The recipe assumes Claude Code runs locally with read access to the user's worktree; the brief and source facets stay on disk.
- Do not let the runner invent source ULIDs. The brief is the authoritative source list. Any artifact whose body lists a ULID outside `tessera playbook sources <target>` fails the V0.5-P6 `tessera check context` integrity check.

## Recipe 2 — Local LLM (Ollama-class)

**Runner:** A local-LLM stack such as Ollama, llama.cpp, or vLLM running a chat model on the user's machine.
**Compiler-version stub:** `ollama/<model>-recipe@YYYY-MM-DD` or `llamacpp/<model>-recipe@YYYY-MM-DD`.

This recipe is the air-gapped equivalent of Recipe 1. The brief and source facets travel through the local model context window; nothing leaves the host. The recipe trades the interactive Claude Code surface for a scripted compile loop.

### Steps

1. **Generate the brief.**

   ```bash
   tessera playbook scaffold swcr_design_brief --out .scratch/swcr.brief.md
   ```

2. **Pull every source facet body.** A small wrapper script reads the brief's source-facet table and fetches every source through the REST surface. `tessera curl show <ulid>` wraps `GET /api/v1/facets/<ulid>` and returns the full facet body, not the 200-character snippet that `tessera playbook sources --json` carries. The output is a concatenation of per-source JSON objects (newline-separated), not a single JSON document — name the file accordingly so a `jq`-style consumer does not assume valid JSON.

   ```bash
   tessera playbook sources swcr_design_brief --json \
       | jq -r '.sources[].external_id' \
       | while read ulid; do
             tessera curl show "$ulid"
         done > .scratch/swcr.sources.ndjson
   ```

3. **Compose the prompt.** The prompt is the scaffold brief verbatim plus the source bundle plus a fixed instruction block: "Produce a Markdown document with the seven `## ` headings listed in §Required output sections. Cite source ULIDs from the source-facet table. Do not invent source IDs." Local recipes pick a single instruction block per recipe version and keep it under source control alongside `docs/playbook-compiler-recipes.md`.
4. **Run the model.**

   ```bash
   ollama run llama3.1:70b "$(cat .scratch/swcr.brief.md \
       .scratch/swcr.sources.ndjson \
       prompts/swcr-recipe-2026-05-09.txt)" \
       > .scratch/swcr.playbook.md
   ```

5. **Run the eval set.** Local-LLM recipes score evals through the same model in a separate sub-prompt: "Answer this question using only the artifact body" and "List the `expected_claims` the answer preserves." Record results under `## Eval summary` in the artifact body.
6. **Register the artifact.**

   ```bash
   tessera playbook register swcr_design_brief \
       --content .scratch/swcr.playbook.md \
       --compiler-version "ollama/llama3.1-70b-recipe@2026-05-09"
   ```

7. **Recompile when stale.** Same staleness loop as Recipe 1.

### Boundary reminders

- The model size matters for source coverage. A recipe that drops sources from the prompt because of a context-window cap must record that under `## Known gaps`. Hidden truncation breaks the V0.5 contract that the compile reads every tagged source.
- Local-LLM recipes pin the prompt file under source control. The compiler-version date must match the prompt-file date; otherwise the version stamp lies about what was compiled.

## Recipe 3 — No-LLM manual authoring

**Runner:** A human editor.
**Compiler-version stub:** `manual/<recipe-name>@<semver-or-date>`.

This recipe exists for high-stakes Playbooks where the user does not trust either Claude Code or a local model to draft the synthesized operating model. The cost is human time; the win is that the human is the compiler, so the artifact is exactly what the human believes the answer is.

### Steps

1. **Generate the brief.**

   ```bash
   tessera playbook scaffold tessera_release_playbook --out .scratch/release.brief.md
   ```

2. **Read the source facets.** Fetch each ULID in the brief's source-facet table with `tessera curl show <ulid>` (or open the disk-backed Markdown when the source is a project-context section synced from the repo).
3. **Author the artifact body in Markdown.** Write the seven required sections. The synthesized operating model is the part the human owns; the inventory and provenance sections quote the source ULIDs the author actually read.
4. **Run the eval set.** For each eval entry, the author answers the question against the artifact body without re-reading sources, then scores their own answer's `expected_claims`. Manual evals are slow and that is the point: the friction surfaces missing claims faster than a model would.
5. **Register the artifact.**

   ```bash
   tessera playbook register tessera_release_playbook \
       --content .scratch/release.playbook.md \
       --compiler-version "manual/release-recipe@1.0.0"
   ```

6. **Recompile when stale.** Manual recipes treat staleness as a review prompt, not an automatic recompile. The author re-reads only the mutated sources surfaced by `tessera playbook stale --json`, decides whether the change actually invalidates the synthesis, and either edits the affected sections or registers a fresh artifact.

### Boundary reminders

- Manual authoring still claims a `compiler-version`. Bumping the semver per substantive rewrite preserves audit-log readability across recompiles even though no model executed.
- The author is responsible for not inventing claims. The provenance section is the integrity gate; if a claim cannot trace to a source ULID, either cite a new source (and add it to `metadata.compile_into`) or move the claim to `## Known gaps`.

## Recording field-level provenance and eval metadata

The CLI register path stores `eval_summary` and `field_provenance` only inside the artifact body for V0.5. Recipes that need machine-readable metadata on `compiled_artifacts.metadata` register through the Python API:

> **Internal-API caveat.** The example below imports `open_vault` and `resolve_agent_id` from `tessera.cli._common`. The leading underscore is intentional: that module is the CLI's helper layer and is not a stable public surface. Recipes that bind to it should pin the Tessera version and treat the import as a sharp edge until a `tessera playbook register --metadata <json>` flag (or an equivalent public Python API) lands. The flow is documented here because metadata-bearing registration is otherwise unreachable through the V0.5 CLI.

```python
from tessera.cli._common import open_vault, resolve_agent_id
from tessera.vault import compiled as vault_compiled

with open_vault(vault_path, passphrase) as vc:
    agent_id = resolve_agent_id(vc.conn, explicit=None)
    external_id = vault_compiled.register_compiled_artifact(
        vc.conn,
        agent_id=agent_id,
        content=artifact_body,
        source_facets=source_ulids,
        artifact_type="playbook",
        compiler_version="claude-code/release-recipe@2026-05-08",
        source_tool="claude-code",
        metadata={
            "eval_summary": {
                "target": "release_playbook",
                "compiler": "claude-code/release-recipe@2026-05-08",
                "passed": 7,
                "failed": 1,
                "skipped": 0,
                "must_failures": [],
            },
            "failed_evals": [
                {
                    "id": "release-staleness",
                    "severity": "should",
                    "reason": "answer named the staleness rule but did not cite the V0.5-P7 contract",
                }
            ],
            "field_provenance": {
                "Synthesized operating model": {
                    "source_facets": ["01J0A...", "01J0B..."],
                    "source_refs": [
                        {
                            "path": "docs/release-spec.md",
                            "section": "v0.5",
                            "ref_kind": "supports",
                        }
                    ],
                    "confidence": "high",
                }
            },
        },
    )
```

The metadata dict is stored under `caller_metadata` per the daemon's pair-write contract, alongside the four daemon-owned keys (`artifact_type`, `compiler_version`, `source_facets`, plus `is_stale` on the row). Subset semantics are documented but not enforced at write time today; recipe authors validate them through `tessera check context` once V0.6 lands the integrity check.

## Validation checklist for the recipe pack

Phase 8 of `.docs/compiled-playbooks-enhancement-plan.md` ships when one recipe in this pack has produced a registered artifact from real Tessera facets, the artifact has been retrieved through `recall`, and a source mutation has flipped staleness on it. Recipe authors record evidence under `docs/dogfood/` rather than this document; the dogfood log is the load-bearing artifact for the gate, not the recipe doc.

| Gate | Owner | Evidence path |
| --- | --- | --- |
| One recipe produces a registered artifact from real Tessera facets | Recipe author | `docs/dogfood/compiled-notebook-dogfood.md` evidence log row |
| The artifact can be retrieved through `recall` | Recipe author | `tessera recall` output recorded in the dogfood evidence log |
| Staleness flips when a source mutates | Recipe author | `tessera playbook stale --json` output before and after the source mutation |

Recipes outside this pack are welcome. New recipes either land in this document under a new `## Recipe <n>` heading or live in a downstream caller's repo with a backlink. Either way, the runner-name / recipe-name / version convention and the seven minimum sections are the V0.5 contract; recipe authors do not get to renegotiate either through prose alone.
