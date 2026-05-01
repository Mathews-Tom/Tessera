# ADR 0019 — Compiled notebook as the AgenticOS Playbook

**Status:** Accepted
**Date:** May 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0010](0010-five-facet-user-context-model.md), [ADR 0011](0011-swcr-default-on-cross-facet-coherence.md), [ADR 0017](0017-agent-profile-facet.md), [ADR 0018](0018-verification-retrospective-facets.md), [ADR 0021](0021-audit-chain-tamper-evidence.md), AgenticOS workshop §Project 10 (Playbook), `docs/release-spec.md §v0.5`, `docs/system-design.md §v0.5`

## Context

ADR 0010 reserved `facet_type='compiled_notebook'` in the schema v2 CHECK constraint and reserved the `compiled_artifacts` table empty but present. The intent at the time was Karpathy-style write-time synthesis for vertical-depth research topics. The reservation was correct. The shape was deliberately left open.

The AgenticOS workshop §Project 10 names a different artifact at the same conceptual layer: **the Playbook** — "a single document capturing your entire system: what you built, what's working, how your agents relate, and what you'd build next." The Playbook is the operating manual of the user's AgenticOS. It binds layers 1 through 8 (identity, context, skills, memory, connections, agents, verification, automations) into one recallable narrative. It is the document a user would hand to a colleague who asks "how do you work with AI?"

These two artifacts — vertical-depth research synthesis and AgenticOS Playbook — share infrastructure: both are write-time-compiled, both are bundles of multiple source facets, both want SWCR to surface them at the right moment, both go stale when sources mutate, and both need the same mode semantics (`mode='write_time'`, ADR-0010). The schema reservation can host either. The design question is whether to commit to one shape or two.

The decision below commits to **one type, one compiler, one storage table — with the shape unified around the AgenticOS Playbook framing**. The vertical-depth research case becomes a degenerate input to the same compiler (a Playbook with one source `project` and one source `skill` is a vertical-depth synthesis; a Playbook with sources spanning every layer is the workshop's Project 10). One artifact type, one retrieval surface, one staleness story.

The unification is also a positioning lever: "compiled notebook" reads as a private artifact for one user's research; "AgenticOS Playbook" reads as the operating manual for the user's portable AI setup. The latter matches the v0.5 reframe. The schema name (`compiled_notebook`) stays for backward compatibility with the v2 reservation; user-facing prose and CLI affordances refer to the Playbook.

## Decision

`facet_type='compiled_notebook'` is the AgenticOS Playbook. One concrete shape, one compiler, one storage path.

### Source facet inputs

The compilation agent reads tagged sources from the user's vault. v0.5 admits four source types:

| Source type            | Role in the Playbook                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------------ |
| `agent_profile`        | Per-agent purpose, inputs, outputs, cadence (ADR 0017)                                     |
| `project`              | Active work context, decisions, current state                                              |
| `skill`                | Reusable procedures the user has authored (ADR 0012)                                       |
| `verification_checklist` | Pre-delivery gates per agent (ADR 0018)                                                  |

`identity`, `preference`, `workflow`, `style`, `person`, `retrospective`, `automation` rows are **not** primary inputs but are admissible as **context shapers**: SWCR surfaces them through the standard cross-facet recall when the compiler queries the vault, and they shape the synthesized prose without becoming explicit sections of the artifact.

The user tags sources via metadata on the source facet itself (`metadata.compile_into = ['playbook_main']` or equivalent). The compiler reads tagged rows by querying `facets WHERE metadata->>'compile_into' = ?`. No separate compile-membership table.

### Compilation pipeline

```
tag sources → trigger compile (manual or staleness-driven)
            → compiler reads tagged sources via recall(facet_types=[agent_profile, project, skill, verification_checklist])
            → compiler synthesizes narrative artifact at write time
            → compiler writes a new facet (facet_type='compiled_notebook', mode='write_time')
            → compiler writes a row in compiled_artifacts with the rendered content + source_facets list
            → audit_log_append records the compile event
```

The compiler is **out of process** — Tessera does not embed an LLM. The compiler is whichever caller-side runner the user wires up (Claude Code, OpenClaw, a custom script). Tessera exposes:

- `recall(facet_types=[…], compile_target='playbook_main')` — returns the source facets the compiler should consume.
- `register_compiled_artifact(external_id, content, source_facets, compiler_version, …)` — stores the result and writes the matching `compiled_notebook` facet in one transaction.

The two-call shape is deliberate: the compiler can be any process with a token; Tessera does not run the compiler, just stores its output. This mirrors ADR 0018's storage-vs-execution boundary on verification checklists and ADR 0020's storage-only stance on automations.

### Retrieval surface

`recall(facet_types=all)` returns `compiled_notebook` facets when relevant. Two metadata fields on the response shape it:

- `mode='write_time'` — the response carries the row's production method so callers can present it differently (e.g., as a synthesized brief rather than raw context).
- `is_stale=0|1` — staleness flag set when source facets mutate (V0.5-P6 owns the staleness wiring; ADR 0019 commits the field's existence).

The bundle's token budget envelope treats compiled artifacts as one more facet type competing with the others. SWCR coherence weights apply uniformly; there is no privileged slice.

### Storage

`compiled_artifacts` (already present and empty in schema v3) gains rows. Schema unchanged from v3:

```
compiled_artifacts(
  id, external_id, agent_id, source_facets, artifact_type,
  content, compiled_at, compiler_version, is_stale, metadata
)
```

Each row is paired with a `compiled_notebook` facet whose `external_id` matches `compiled_artifacts.external_id`. The facet carries the recallable surface; `compiled_artifacts` carries the rendered content + provenance.

### Boundary statement

**Tessera stores compiled artifacts; the caller compiles them.** No in-process LLM, no compiler runtime in the daemon, no hosted compilation service. The `register_compiled_artifact` call is the only write path; no `compile_now()` API exists.

## Rationale

1. **One type, two use cases, unified compiler.** Vertical-depth research synthesis and AgenticOS Playbook share infrastructure; splitting them would invent a parallel facet type, parallel storage table, and parallel staleness story for no benefit. Unification keeps the surface narrow.
2. **Schema name (`compiled_notebook`) survives.** The v2 schema CHECK already admits the value. Renaming to `playbook` would force a CHECK constraint rewrite and a migration; the v2 reservation is the cheaper path.
3. **User-facing prose calls it the Playbook.** The CLI (`tessera playbook list`, `tessera playbook compile`), the docs, the release notes, and `recall` response metadata refer to playbooks. Internal schema and module names stay `compiled_notebook` / `compiled_artifacts`. This is the same convention as the post-reframe codebase: the schema names predate the reframe; the user-facing prose follows the reframe.
4. **Out-of-process compiler.** ADR 0014 made Tessera ONNX-only by removing every external runtime; this ADR restores out-of-process generation through a different boundary. Tessera does not embed an LLM. The compiler is the caller's runtime. This boundary is shared with ADR 0018 (Tessera stores checklists; callers execute them) and ADR 0020 (Tessera registers automations; callers run them).
5. **Two-call API (`recall` + `register_compiled_artifact`).** A one-call `compile_playbook` API would couple the daemon to whichever LLM the user picked — exactly what ADR 0014 rejected for embedders. The two-call shape lets callers pick any compiler, including local-only ones.
6. **`is_stale` is a flag, not a re-compile trigger.** Tessera flags staleness; the compiler decides when to act. This keeps the daemon's responsibility narrow (audit emission, source-mutation detection) and pushes the compile-cadence decision to the caller (V0.5-P6 owns the wiring).
7. **Source-tag pattern over a membership table.** Storing `compile_into=['playbook_main']` on the source facet's metadata avoids a parallel membership table and lets SWCR continue to use the existing facet shape. Multiple playbooks per vault are addressable through the array.
8. **Ship-gated on V0.5-P8 (audit chain).** Write-time mode introduces synthesized state that did not exist in any source. A defensible audit story is mandatory before that surface reaches users. This ADR (and ADR 0021) commit the gate so V0.5-P4 cannot ship before the chain is verified.

## Consequences

**Positive:**
- AgenticOS Layer 6 (Agents) + Layer 7 (Verification) + Project 10 (Playbook) all surface through one artifact and one retrieval path.
- Schema is already ready. v0.5 adds rows to `compiled_artifacts` and `compiled_notebook` facets; no table additions for this ADR.
- Compiler is replaceable. Users on Claude Code, OpenClaw, Cursor, or custom scripts all use the same `register_compiled_artifact` API.

**Negative:**
- Source-tag pattern requires the user (or the calling tool) to mark sources. There is no auto-discovery; a Playbook never compiles itself. This is intentional — it matches Tessera's no-auto-capture ideology bar — but it means the first compile is a deliberate user act, not an emergent product feature.
- Two-call API places coordination on the caller. A buggy caller can call `register_compiled_artifact` without first reading sources, producing a degenerate artifact. The MCP boundary validates the call shape; the artifact's quality is the caller's responsibility.
- Internal-name vs. user-facing-name divergence (`compiled_notebook` schema, "Playbook" prose) requires consistent docs. The CLI and release notes carry the Playbook framing; the schema and module names stay backward-compatible.
- V0.5-P4 cannot merge to `main` before V0.5-P8's audit chain is green. This is non-negotiable per the v0.5 sequencing constraint and ADR 0021.

## Alternatives considered

- **Two facet types (`compiled_notebook` for research synthesis, `playbook` for AgenticOS).** Rejected. Splits one artifact into two; doubles staleness, retrieval, and storage paths for no gain.
- **In-process compiler.** Rejected. Couples the daemon to a specific LLM and runtime; reintroduces the dependency closure ADR 0014 just removed.
- **One-call `compile_playbook(target_external_id)` API.** Rejected. Forces the daemon to choose the LLM; conflicts with the boundary ADR 0014 established.
- **Auto-compile on source mutation.** Rejected. Re-introduces auto-capture-style behavior the ideology bars in `docs/non-goals.md` reject. Stale flagging plus user-driven re-compile preserves user control.
- **Drop the `compiled_notebook` facet type entirely; store playbooks in `project`.** Rejected. Conflates write-time and query-time semantics, defeats the v2 reservation, and forces SWCR to special-case the type by metadata shape.
- **Rename schema CHECK from `compiled_notebook` to `playbook`.** Rejected. Forces a CHECK constraint migration to the schema for a cosmetic gain. The v2 reservation is honored; user-facing prose carries the Playbook framing without a schema rewrite.

## Revisit triggers

- Real-user data shows users compile playbooks once and never recompile. Either the staleness signal is too weak or the artifact is too expensive to recompile; investigate which.
- A second artifact type emerges that does not fit the Playbook frame (e.g., automatically-generated weekly digests). Consider a new facet type rather than overloading `compiled_notebook`.
- The two-call API produces consistent caller bugs (writing artifacts without reading sources). Tighten the boundary by validating `source_facets` against actual recently-recalled facets at the MCP layer.
- LLM-side runtimes converge on a standard "compile this bundle" interface. Tessera adds a thin recipe (similar to `tessera curl`) without taking on execution.

## Related documents

- `docs/adr/0010-five-facet-user-context-model.md` — `compiled_notebook` reserved here; this ADR gives it shape.
- `docs/adr/0017-agent-profile-facet.md` — agent profiles are a primary source.
- `docs/adr/0018-verification-retrospective-facets.md` — verification checklists are a primary source.
- `docs/adr/0021-audit-chain-tamper-evidence.md` — audit chain ship-gate before write-time mode reaches users.
- `docs/release-spec.md §v0.5` — DoD bullets for compiled artifacts and the Playbook.
- `docs/system-design.md §v0.5` — write-time mode and `compiled_artifacts` table; the Playbook framing is added in V0.5-P4.
- `docs/migration-contract.md` — V0.5-P4 schema work is a row-add against existing tables, not a new table.
- AgenticOS workshop §Project 10 (Playbook) — source framing for the user-facing name.
