# ADR 0016 — Memory volatility model

**Status:** Accepted
**Date:** May 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0010](0010-five-facet-user-context-model.md), [ADR 0011](0011-swcr-default-on-cross-facet-coherence.md), `docs/swcr-spec.md`, `docs/non-goals.md`, AgenticOS workshop §Layer 4 (Memory)

## Context

Tessera v0.4 treats every facet as persistent. A row written into the vault stays until the user soft-deletes it, and SWCR's coherence weighting does not distinguish between a row captured five minutes ago and a row captured eight months ago. That model is correct for `identity`, `preference`, `workflow`, and `style` — those facets are stable for months or years by construction. It is wrong for the AgenticOS workshop's Layer 4 (Memory): "what your tool remembers across sessions." Working memory inside a single agent run, ad-hoc notes that should expire at end-of-session, and ephemeral observations that the user explicitly does not want surviving past today all map to a different lifecycle than the canonical five facets.

The reframe ADRs (0010, 0011) handled the **type** axis: what kind of context lives where. They left the **lifecycle** axis untyped. Real-user dogfooding of v0.4 surfaced the gap as repeated friction: every captured `project` row competes with every other `project` row regardless of when it was captured, so SWCR-coherent bundles trend toward whatever the user thought about most often, not whatever is most current. AgenticOS Layer 4 names the gap directly.

The v0.5 reconciliation introduces volatility as a first-class column on `facets` rather than as a per-facet-type policy. Volatility is orthogonal to facet type: a `project` facet may be persistent (long-running engagement) or session-scoped (a single sprint's working notes); a `style` facet is virtually always persistent; a workshop-only working note that should evaporate at midnight is `ephemeral` regardless of which facet type it nominally fits.

## Decision

Add a `volatility` column to `facets`:

```sql
volatility TEXT NOT NULL DEFAULT 'persistent'
    CHECK (volatility IN ('persistent', 'session', 'ephemeral'))
```

Three values, fixed at v0.5:

| Value        | Lifecycle                                                                 | TTL default                                       | SWCR freshness weight                              |
| ------------ | ------------------------------------------------------------------------- | ------------------------------------------------- | -------------------------------------------------- |
| `persistent` | Default; row survives until user soft-deletes via `forget`                | None                                              | None (current SWCR behavior)                        |
| `session`    | Auto-compacted when the captor's `agent_id` has had no activity for 24 h   | 24 h sliding (configurable per agent)             | Linear decay over the TTL window                    |
| `ephemeral`  | Auto-compacted at next daemon idle sweep after the TTL                    | 60 min sliding (configurable, max 24 h)           | Step decay (full weight inside TTL, hard zero past) |

Auto-compaction is a soft-delete (existing audit-emitting path), not a hard delete: forensic visibility is preserved through `audit_log` and the row remains queryable through `show --include-deleted`. Hard-delete cascade across `vec_<id>` tables runs on the same schedule as the existing v0.1 hard-delete pipeline.

SWCR consumes volatility through a single `freshness(f)` term added to the per-facet score:

```
s_SWCR(f) = s_r(f) · freshness(f) + λ · Σ w(f, f') · s_r(f') · freshness(f')
```

For `persistent` rows, `freshness(f) = 1.0` and the algorithm reduces to its current form. For `session`/`ephemeral`, freshness decays linearly or step-wise toward 0 across the TTL window. The decay function is closed-form, deterministic per `(captured_at, now, volatility, ttl_seconds)`, and recorded in `docs/swcr-spec.md §Algorithm` alongside the existing `(α, β, γ, λ)` parameters.

Capture writes accept an explicit `volatility` parameter. Default is `persistent`. The MCP/REST surfaces validate against the CHECK constraint and emit `AppendInvalidVolatility` on mismatch. Connectors that capture working memory (Claude Code session notes, OpenClaw HEARTBEAT scratchpads) pass `volatility='session'`; user-driven `tessera capture` defaults to `persistent` and surfaces `--volatility` as an explicit flag.

The v0.3 facet types (`person`, `skill`) and the v0.5-additive types (`agent_profile`, `verification_checklist`, `retrospective`, `compiled_notebook`, `automation`) all default to `persistent` at the type level. A row CAN override to `session`/`ephemeral`, but the type defaults reflect the prevailing lifecycle.

## Rationale

1. **Volatility is orthogonal to type.** Coupling lifecycle to facet type would force the design into one of two failure modes: collapsing every type into one lifecycle (rejected — `style` is not `working_memory`), or fragmenting type-counts into `project_persistent` / `project_session` / `project_ephemeral` (rejected — schema fragmentation, type-explosion, unworkable scopes). A separate column expresses both axes without conflation.
2. **AgenticOS Layer 4 is named, not new.** The workshop's working/persistent split is a vocabulary that already exists in callers. Tessera adopts the vocabulary so caller-side runners (Claude Code, OpenClaw, Cursor) can use one column to mark working memory without a translation layer.
3. **Persistent default protects existing data.** Every row in a v3 vault becomes `volatility='persistent'` at upgrade. The schema migration is additive, idempotent, and lossless. Behavior on every existing vault is identical to v0.4 until the caller writes a non-persistent row.
4. **Soft-delete on auto-compaction preserves forensic posture.** Auto-compaction runs through the existing soft-delete path, not a separate destructive sweep. `audit_log` records `forget` events with `reason='auto_compaction'`. Hard-delete cascade reuses the v0.1 pipeline.
5. **Closed-form freshness keeps SWCR deterministic.** A learned freshness function would break the determinism CI gate (`recall` returns identical IDs for identical inputs at fixed `now`). Linear and step decay are cheap, explainable, and pass determinism tests trivially as long as `now` is captured once per `recall` and threaded through.
6. **Three values, not five.** A `working` / `recent` / `aging` / `cold` / `archived` split was rejected: each additional value forces SWCR to learn a new weight, and the rejected values can be expressed by configuring the TTL on `session`/`ephemeral`. Three values is the minimum that distinguishes persistent / session-scoped / ephemeral and the maximum that does not over-fit to a hypothetical UI taxonomy.

## Consequences

**Positive:**
- AgenticOS Layer 4 maps cleanly onto a single column rather than a parallel data structure or naming convention.
- SWCR bundles for active projects automatically prefer recently captured context, reducing the "stale-bundle" failure mode dogfooding has seen on `project` facets.
- Working memory written by autonomous agents has a defined lifecycle; the vault does not accumulate session debris indefinitely.
- The schema delta is one column with a CHECK and a NOT NULL DEFAULT — additive, idempotent, free to roll forward, free to roll back.

**Negative:**
- SWCR gains a freshness term that callers cannot opt out of (no `swcr_no_freshness` mode at v0.5). Users with primarily `persistent` data see no behavior change; users mixing `session`/`ephemeral` cannot disable decay without rewriting volatility on existing rows.
- The auto-compaction daemon sweep adds a recurring background task. Its schedule, log volume, and failure modes need their own observability story (events emitted to `events.db`).
- Client-side tooling has a new flag to surface (`--volatility`). Documentation must explain the lifecycle without nudging users to overuse `session`/`ephemeral`; the default stays persistent precisely so users never have to think about it.

## Alternatives considered

- **Per-facet-type lifecycle policy.** Rejected. Couples two orthogonal axes; produces type fragmentation if any type needs both lifecycles.
- **Tag-based lifecycle in `metadata` JSON.** Rejected. Bypasses CHECK enforcement, fights the existing schema, and would not be queryable by SWCR without an index.
- **Hard-delete on auto-compaction.** Rejected. Loses forensic posture, fights the existing audit story, and conflicts with the planned ADR-0021 audit-chain claim that every state change is auditable.
- **Learned freshness function.** Rejected at v0.5. Breaks determinism, requires per-user training data Tessera does not collect (ideology bar — no telemetry), and over-fits to assumed user preferences. May be revisited post-v1.0 if real-user signal calls for it.
- **Two values (`persistent` / `volatile`).** Rejected. Conflates session-scoped and ephemeral lifecycles; callers handling both regimes (Claude Code session notes vs. midnight-evaporation observations) need distinct values.

## Revisit triggers

- Real-user data shows `session` and `ephemeral` distributions are statistically indistinguishable. Collapse to a two-value model in a follow-up ADR.
- A caller uniformly writes one volatility value across all captures. Investigate whether the type default is wrong, not whether the column is wrong.
- Closed-form linear/step decay produces measurably worse SWCR coherence than `persistent`-only on a head-to-head ablation. Move the freshness function behind a config switch and reopen the algorithm.
- Auto-compaction load on the embed pipeline regresses B-EMB-1 by more than 15%. Tighten the sweep schedule or move auto-compaction off the embed path.

## Related documents

- `docs/system-design.md §The five-facet context model` — extended in V0.5-P1 with the volatility section.
- `docs/swcr-spec.md §Algorithm` — extended in V0.5-P1 with the `freshness(f)` term and decay function specification.
- `docs/migration-contract.md` — the v3 → v4 migration step list adds the additive `volatility` column with `NOT NULL DEFAULT 'persistent'`.
- `docs/release-spec.md §v0.5` — DoD bullet for the volatility column and SWCR freshness integration.
- `docs/non-goals.md` — confirms learned/telemetry-driven freshness functions remain out of scope.
- `docs/adr/README.md` — index updated with this entry.
