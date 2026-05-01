# ADR 0020 — Automation registry, storage-only

**Status:** Accepted
**Date:** May 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0010](0010-five-facet-user-context-model.md), [ADR 0013](0013-rest-surface-alongside-mcp.md), [ADR 0017](0017-agent-profile-facet.md), [ADR 0018](0018-verification-retrospective-facets.md), [ADR 0019](0019-compiled-notebook-as-agenticos-playbook.md), AgenticOS workshop §Layer 8 (Automations), `docs/non-goals.md`

## Context

AgenticOS workshop §Layer 8 names "scheduled tasks and triggers that let your agents work without you" — daily summaries, hourly monitors, event-driven triggers, anything that runs without a human in the loop. Every modern agentic tool exposes an execution path for this layer: OpenClaw has `HEARTBEAT.md`, Claude Code has `/schedule`, Cursor has automations, cron has cron. Layer 8 is well covered on the **execution** side.

The gap is on the **registry** side. Each runtime stores its own automations in its own format. There is no portable record of "what does the user have running, where, and on what cadence." A user with three agentic tools has three uncorrelated automation lists. Tessera's portability claim — context that travels across every MCP-speaking tool — extends naturally into this gap: register the automation as a recallable facet, then any tool with a token can read the registry, and the user has one list of running things across every runtime.

The risk is scope creep. The same registry could grow into an execution engine: cron-style scheduling inside the daemon, in-process triggers, outbound webhooks to fire off a run. Every one of those steps takes Tessera further from its narrow brief. The non-goals document is explicit:

- No outbound calls except configured adapters (model adapters and BYO sync at v0.5+).
- No scheduler runtime.
- No in-process plugin API.
- No agent runtime.

ADR 0020 commits to **storage-only**: automations are facets; runners are caller-side; Tessera does not ship a scheduler.

## Decision

Add `facet_type='automation'` to the v0.5 facet vocabulary. The registry is durable and recallable; execution is delegated entirely to caller-side runners.

### Facet shape

```
facet_type    : 'automation'
external_id   : caller-supplied stable id (e.g. 'digest_daily')
content       : human-readable description of what the automation does (markdown)
metadata      : {
  agent_ref      : external_id of the owning agent_profile facet (per ADR 0017)
  trigger_spec   : free-form trigger description (cron expr, 'on_event:<name>', 'webhook')
  cadence        : human-readable cadence ('daily 09:00', 'hourly', 'on demand')
  runner         : free-form runner identifier ('claude_code_schedule', 'openclaw_heartbeat', 'cron', 'systemd_timer', 'github_actions')
  last_run       : optional ISO-8601 timestamp of the most recent run (caller-updated)
  last_result    : optional 'success' | 'partial' | 'failure' | string for the most recent run
}
```

`mode='query_time'`, default `volatility='persistent'`. The metadata is documentary, not executable. `trigger_spec` is opaque to Tessera; the runner parses it.

### Runner integration pattern

Caller-side runners read the registry via existing transport surfaces:

- **REST.** `GET /api/v1/facets?facet_type=automation` returns the registered automations. The runner filters by `metadata.runner` to pick its own.
- **MCP.** `recall(facet_types=['automation'])` returns the same list as a context bundle.
- **CLI recipes.** `tessera curl get-facets --facet-type automation` produces a copy-pasteable `curl` invocation for hooks and shell scripts (per ADR 0013).

After a run, the runner updates `last_run` and `last_result` via a normal facet write. Tessera does not push notifications, fire webhooks, or schedule the next run.

### Two new MCP tools (REST parity)

| Tool                       | Scope                  | Behavior                                                     |
| -------------------------- | ---------------------- | ------------------------------------------------------------ |
| `register_automation`      | `write:automation`     | Creates an automation facet; idempotent on `external_id`     |
| `record_automation_run`    | `write:automation`     | Updates `last_run` + `last_result` on an existing automation |

`list_automations` and `get_automation` are not separate tools at v0.5 — `recall(facet_types=['automation'])`, `list_facets(facet_type='automation')`, and `show(external_id)` cover the read paths through the existing surface. The two write tools exist because their distinct scopes (create vs. update-status) document the operation; collapsing them would obscure the scope check.

### Boundary statement

**Tessera registers automations as data; runners execute them.** No scheduler runtime, no outbound triggers, no in-process timer. The daemon does not learn about the existence of an automation until a caller writes it; it does not act on the existence of an automation; it does not fire off a run when one is "due." The runner — whichever caller-side process the user wires up — owns the run-loop.

This boundary is non-negotiable. If a future feature request introduces a scheduler runtime or an outbound trigger inside the daemon, that proposal opens a new ADR and re-litigates this one. The non-goals language ("no scheduler runtime," "no outbound calls except configured adapters") is the canonical source.

## Rationale

1. **Cross-runtime portability is the registry's whole job.** Users with multiple agentic tools have multiple automation lists. A storage-only registry portable across every MCP/REST-speaking tool is exactly what no other runtime ships, because every runtime is biased toward executing its own format. Tessera's unique posture (storage, not execution) makes the registry useful precisely because it is runner-neutral.
2. **`runner` field is informational, not load-bearing.** Tessera does not validate the value, dispatch on it, or index by it. A new runner emerges (Antigravity, a custom Bash script) and the registry accommodates it without code change.
3. **Two write tools, generic read paths.** The write tools document the operation (create vs. update-status); the read paths are already covered by the generic surface (`recall`, `list_facets`, `show`). Adding `list_automations` and `get_automation` as separate tools would duplicate scope-check surface for a thin convenience.
4. **`last_run` / `last_result` updated by the runner, not the daemon.** Tessera has no clock for automations. The runner is the source of truth for "did this fire and what happened." The daemon stores the receipt.
5. **No `next_run` field.** A computed `next_run` would imply Tessera knows when to fire. The runner computes and acts on `next_run`; the registry records what already happened (`last_run`), not what is scheduled to happen. This deliberate omission anchors the storage-vs-execution boundary.
6. **One sub-phase to ship the registry.** V0.5-P5 is intentionally small (0.5 / 1.0 / 1.5 SDW). The shape is deliberately stable; the parts that change with real-user signal are runner-side, not Tessera-side.
7. **`agent_ref` ties the automation back to the AgenticOS Playbook.** A Playbook synthesizes from `agent_profile` + `project` + `skill` + `verification_checklist` (ADR 0019). Automations are reachable from the same anchor (`agent_profile.external_id`) so a Playbook compile naturally surfaces "what is this agent automated to do."
8. **Volatility default is `persistent`.** Automations describe ongoing engagements; a session-scoped automation makes no sense. Callers may override per row, but the default reflects the prevailing case.

## Consequences

**Positive:**
- AgenticOS Layer 8 is covered in storage without taking on execution.
- Cross-runtime portability — one registry, many runners — emerges from existing transport surfaces (REST, MCP, `tessera curl`).
- The boundary against scheduler/outbound surfaces stays explicit, in writing, and in the non-goals canon.
- Playbook compiles include "running automations" naturally because the `automation` facet links to the same `agent_profile` source.

**Negative:**
- The registry is only as accurate as the runner that updates it. A runner that fires but never calls `record_automation_run` produces a stale `last_run`. Users will see this; documentation must explain it.
- Users wanting "Tessera, run my daily summary" find the boundary frustrating. The docs must redirect them to whichever runner they prefer (Claude Code `/schedule`, OpenClaw HEARTBEAT, cron) and provide `tessera curl` recipes that those runners can paste.
- Cross-runtime correlation is opt-in. A runner that does not consult the registry produces unregistered automations (an OpenClaw HEARTBEAT that never reads the registry simply runs without a Tessera record). The user must wire the runner up to the registry; there is no auto-discovery.

## Alternatives considered

- **Built-in scheduler in the daemon.** Rejected. Violates the non-goals ideology bar against scheduler runtimes; introduces an outbound surface; re-introduces the in-process plugin problem.
- **Outbound webhook triggers (Tessera POSTs to a runner when a cadence elapses).** Rejected. Violates the non-goals ideology bar against outbound calls except configured adapters; widens BYO sync into a generic outbound surface.
- **Parallel `automations` table outside `facets`.** Rejected. Bypasses the recall integration that gives the registry its cross-tool portability; doubles the storage and audit surface.
- **Single `manage_automation` MCP tool (covers create/update/delete).** Rejected. Collapses scopes; obscures the write-vs-update-status distinction the two-tool shape documents.
- **`next_run` computed from `trigger_spec`.** Rejected. Tessera does not parse `trigger_spec` — it is opaque metadata. Computing `next_run` requires a parser that turns into a scheduler.
- **Separate facet type per runner (`claude_schedule`, `cron_job`, `openclaw_heartbeat`).** Rejected. Fragments the type vocabulary by transport rather than by data shape; defeats cross-runtime portability.

## Revisit triggers

- Real-user data shows users want a `next_run` field even though Tessera does not compute it. Add a caller-supplied `next_run` field; do not compute it in the daemon.
- A common runner (Claude Code, OpenClaw, Cursor) ships first-class registry integration that removes the need for `tessera curl` recipes. Document the integration as the canonical pattern.
- A user-driven RFC argues the storage-only stance is too narrow. Re-evaluate non-goals; do not silently extend the boundary in code.
- `record_automation_run` calls outpace `register_automation` calls by an order of magnitude. The runner is using `last_run` heavily; consider exposing a dedicated `last_run` index for fast queries without extending the type schema.

## Related documents

- `docs/adr/0010-five-facet-user-context-model.md` — facet-type vocabulary; this ADR adds `automation`.
- `docs/adr/0013-rest-surface-alongside-mcp.md` — REST surface used by caller-side runners.
- `docs/adr/0017-agent-profile-facet.md` — `agent_ref` references its `external_id`.
- `docs/adr/0019-compiled-notebook-as-agenticos-playbook.md` — Playbook compiles surface registered automations through SWCR.
- `docs/non-goals.md` — confirms the storage-only boundary; canonical source for the non-execution stance.
- `docs/release-spec.md §v0.5` — DoD bullets for the registry and the two MCP tools.
- `docs/migration-contract.md` — V0.5-P5 schema delta is one CHECK addition; no new tables.
- AgenticOS workshop §Layer 8 (Automations) — source framing.
