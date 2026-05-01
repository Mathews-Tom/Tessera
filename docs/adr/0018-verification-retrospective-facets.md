# ADR 0018 — Verification + retrospective facets

**Status:** Accepted
**Date:** May 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0010](0010-five-facet-user-context-model.md), [ADR 0011](0011-swcr-default-on-cross-facet-coherence.md), [ADR 0016](0016-memory-volatility-model.md), [ADR 0017](0017-agent-profile-facet.md), AgenticOS workshop §Layer 7 (Verification), `docs/swcr-spec.md`

## Context

AgenticOS workshop §Layer 7 names "how you check and trust what your agents produce." Two distinct artifacts live at that layer:

- **Verification checklist.** A pre-delivery gate the agent runs before declaring a task done. "Did I cover the test cases? Did I update the changelog? Did I check the threat model?" Each item is a yes/no check; the checklist is referenced by `agent_profile.verification_ref` (per ADR 0017).
- **Retrospective.** A post-run record of what worked, what didn't, and what the agent or the user wants to change next time. "The summary missed the migration risk. Add a step to scan for `ALTER TABLE` next run."

Both are recallable user context. Both want SWCR to surface them at the right moment: the checklist when the agent is about to deliver, the retrospective when the agent (or a successor agent) is about to start the same kind of task. Neither belongs in `agent_profile` — a profile describes the agent statically; a checklist is a run-gate; a retrospective is per-run history.

Treating them as inert metadata on `agent_profile` would conflate three lifecycles (profile rev, checklist rev, retrospective accumulation) onto one row and make SWCR unable to distinguish them at retrieval time. Treating retrospectives as `project` facets would lose the agent linkage and dilute SWCR's coherence weighting against the agent's own past runs. Both failure modes are foreseeable; the right shape is two new facet types with explicit linkage to the owning agent profile.

The downstream consequence that makes this ADR worth writing now (rather than at V0.5-P3 implementation time) is **the SWCR retrospective integration**: retrospectives feed back into the SWCR coherence graph as high-coherence prior context for the next run of the same agent. That integration changes the SWCR algorithm specification. Locking the contract at the ADR layer means V0.5-P3 implementation does not relitigate it.

## Decision

Add two new facet types to the v0.5 vocabulary:

### `verification_checklist`

```
facet_type    : 'verification_checklist'
external_id   : caller-supplied stable id (e.g. 'digest_v3_pre_delivery')
content       : human-readable narrative of what the checklist covers (markdown)
metadata      : {
  agent_ref      : external_id of the owning agent_profile facet
  trigger        : free-form when-to-run string ('pre_delivery', 'before_commit', cron expr)
  checks         : list of {id, statement, severity} objects
  pass_criteria  : free-form description of when the checklist counts as passed
}
```

`checks[].severity` is one of `blocker | warning | informational`. Presence of any failed `blocker` check fails the gate; failed `warning` items surface to the user without blocking; `informational` items annotate the run.

`mode='query_time'`, default `volatility='persistent'`. Multiple checklists may exist per agent profile (different triggers); `agent_profile.verification_ref` references the canonical one.

### `retrospective`

```
facet_type    : 'retrospective'
external_id   : caller-supplied stable id (e.g. 'digest_v3_run_2026_05_03')
content       : human-readable narrative of what the run did and how it went (markdown)
metadata      : {
  agent_ref      : external_id of the owning agent_profile facet
  task_id        : caller-supplied identifier of the task this retrospective covers
  went_well      : list of strings
  gaps           : list of strings
  changes        : list of {target, change} objects describing requested edits to the agent profile, checklist, skill, or related facets
  outcome        : 'success' | 'partial' | 'failure'
}
```

`mode='query_time'`, default `volatility='persistent'`. Retrospectives accumulate over time; the user (or the agent itself) decides whether stale retrospectives should be re-volatilized to `volatility='session'` or `forget`-ed once their lessons are absorbed into the profile or checklist.

### SWCR retrospective integration

When `recall(facet_types=all)` returns candidates that include an `agent_profile` facet, the SWCR coherence graph is augmented with the **most recent N retrospectives whose `agent_ref` matches that profile** (default N = 3, configurable via `retrieval.swcr.retrospective_window`). Those retrospective facets enter the candidate set with their normal relevance scores; the cross-type bonus (the γ term in `docs/swcr-spec.md §Algorithm`) treats `agent_profile ↔ retrospective` as a high-coherence edge so the bundle naturally hangs together when the user asks "how is the digest agent doing?"

The integration is closed-form, deterministic given fixed `now`, and does not require new infrastructure beyond the existing SWCR fan-out. The token budget is shared with the existing per-facet-type envelope: retrospectives compete with project / style / workflow rows for the bundle's tokens; they are not allotted a privileged slice.

### Three new MCP tools (REST parity)

| Tool                          | Scope                              | Behavior                                                                |
| ----------------------------- | ---------------------------------- | ----------------------------------------------------------------------- |
| `register_checklist`          | `write:verification_checklist`     | Creates a checklist facet; optionally updates an `agent_profile.verification_ref` |
| `record_retrospective`        | `write:retrospective`              | Creates a retrospective facet linked to an agent profile                |
| `list_checks_for_agent`       | `read:verification_checklist`      | Returns the canonical checklist for an agent profile                     |

Retrospectives are surfaced through `recall(facet_types=all)` and `list_facets(facet_type='retrospective')`; no dedicated `list_retrospectives_for_agent` tool ships at v0.5 because `recall` covers the use case and the additional tool would duplicate scope-check surface for a thin convenience.

### Boundary statement

**Verification is a run-gate, not a guarantee.** Tessera stores the checklist; it does not run the checklist. The agent (or the caller-side runner) reads the checklist via `recall` or `list_checks_for_agent`, executes its checks, and either decides delivery is allowed or aborts. `pass_criteria` is documentation, not enforcement. This boundary mirrors ADR 0020's stance on automations: Tessera registers, callers execute.

## Rationale

1. **Two types, not one.** A "checklist run record" and a "retrospective" share narrative shape but have opposite lifecycles: a checklist exists once per agent and evolves slowly; retrospectives accumulate per-run and are write-once per task. Conflating them means SWCR cannot distinguish "the gate I run before delivering" from "what happened the last time I delivered."
2. **Explicit `agent_ref` over inferred linkage.** A facet's `agent_id` (existing FK on every row) records which agent owns the row. `agent_ref` is a separate metadata field pointing at the **agent profile facet** the artifact relates to. The two diverge precisely because tokens may be issued for service or subagent classes that own checklists describing other agents (orchestrator agents writing checklists for downstream workers).
3. **Retrospectives feed SWCR; they do not replace it.** A learned-feedback loop where retrospectives mutate SWCR weights is rejected at v0.5 (per ADR 0011's stance against learned weights without telemetry). The integration here is structural — retrospectives enter the candidate graph as high-coherence neighbors of the agent profile — not statistical.
4. **`severity` as a fixed three-value enum.** Blocker / warning / informational covers the common case; allowing free-form severities would force callers to invent ad-hoc taxonomies that SWCR cannot weight uniformly. If real-user signal demands a fourth value, open a follow-up ADR.
5. **Three MCP tools, not five.** `register_checklist` covers create-or-replace; `record_retrospective` is write-only by design (immutable per task); `list_checks_for_agent` is the targeted read. Get/list combinations on retrospectives go through the generic `recall` / `list_facets` surface to avoid scope-check duplication.
6. **No checklist execution engine.** Tessera storing-the-checklist plus the caller running-the-checklist mirrors the AgenticOS Layer 8 stance (Tessera registers automations, callers execute) and the existing ideology bar against in-process plugins. Adding an execution engine introduces a new attack surface, a new failure mode, and an unbounded scope of language support. The boundary statement above prevents the slippage.

## Consequences

**Positive:**
- Verification and retrospective both become first-class recallable context. SWCR bundles surface the right gate at the right moment.
- Retrospective integration gives SWCR a structural memory of past runs without inventing a per-user training loop or violating the no-telemetry ideology bar.
- The boundary against execution keeps Tessera narrow; caller-side runners (Claude Code, OpenClaw, autonomous frameworks) own the run-loop.

**Negative:**
- Two facet types added in one sub-phase means V0.5-P3 schema delta touches CHECK constraints, scope allowlists, MCP tools, and the SWCR algorithm in one window. Mitigated by the additive migration step pattern (each type ships its own step) and by ADR-0011's regression-guard invariants on B-RET-1.
- Retrospective accumulation produces a long tail of facets per agent. Callers may need to revolatilize old retrospectives to `volatility='session'` (per ADR 0016) or `forget` them; v0.5 docs the pattern without enforcing it.
- The retrospective window (default N = 3) is a tunable that affects bundle composition. Real-user data will likely call for adjustment; the parameter is exposed in `retrieval.swcr.retrospective_window` so it can be tuned without code changes.

## Alternatives considered

- **One combined facet type (`verification`).** Rejected. Conflicts with two opposite lifecycles. Forces SWCR to special-case the type by metadata shape rather than by type.
- **Inline checklist into `agent_profile.metadata`.** Rejected. Profile evolution and checklist evolution have different cadences. Inlining means every checklist edit invalidates the profile facet's content hash, defeating dedup.
- **Retrospectives in `audit_log`.** Rejected. The audit log is structured around operations (capture, recall, forget, ...), not narrative reflection. Retrospectives need recall-surface visibility, not audit-surface visibility, and putting them in audit conflicts with ADR-0021's tamper-evidence claim (every audit row is hashable; narrative retrospectives invite re-edit cycles).
- **A dedicated `retrospectives` table outside `facets`.** Rejected. Bypasses every advantage of the facet vocabulary: scope checks, recall integration, audit emission, content-hash dedup, soft-delete.
- **Mandatory `verification_ref` on `agent_profile`.** Rejected (per ADR 0017's choice of nullable). Forces every agent to ship with a checklist before it can register; circular dependency between this ADR and ADR-0017.

## Revisit triggers

- Retrospectives in real vaults outnumber every other facet type by an order of magnitude. Either tighten default volatility on the type or surface a `compact_retrospectives` operation.
- The retrospective-window parameter requires per-agent tuning. Move from a single global config to a per-agent override on `agent_profile.metadata`.
- Real-user signal calls for a checklist execution engine inside the daemon. Re-evaluate the boundary stated above against the AgenticOS Layer 8 stance.
- A fourth severity level is consistently used by callers. Open a follow-up ADR; do not silently extend the enum.

## Related documents

- `docs/adr/0011-swcr-default-on-cross-facet-coherence.md` — SWCR's role; this ADR adds the retrospective integration.
- `docs/adr/0017-agent-profile-facet.md` — agent profile; this ADR's `agent_ref` references its `external_id`.
- `docs/adr/0016-memory-volatility-model.md` — both new types default to `volatility='persistent'`.
- `docs/swcr-spec.md §Algorithm` — extended in V0.5-P3 with the retrospective augmentation rule.
- `docs/system-design.md §Retrieval pipeline` — extended in V0.5-P3 with the verification + retrospective surfaces.
- `docs/release-spec.md §v0.5` — DoD bullets for the two facet types and the three new MCP tools.
- `docs/migration-contract.md` — V0.5-P3 schema delta adds CHECK values for both types.
