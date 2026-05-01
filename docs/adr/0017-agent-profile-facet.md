# ADR 0017 — Agent profile as a first-class facet

**Status:** Accepted
**Date:** May 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0007](0007-token-lifecycle.md), [ADR 0010](0010-five-facet-user-context-model.md), [ADR 0016](0016-memory-volatility-model.md), AgenticOS workshop §Layer 6 (Agents), `docs/system-design.md`, `docs/threat-model.md`

## Context

The `agents` table has held two unrelated responsibilities since v0.1:

1. **Authentication principal.** The JWT subject store. Capability tokens reference an `agent_id` so per-token scope checks can attribute every read/write/audit event to the calling tool. The auth pipeline assumes `agents.id` is stable and matches a row.
2. **Implicit identity bag.** Connectors and importers stash `name` and a free-form `metadata` JSON blob on the row. Nothing in the retrieval pipeline consumes that metadata; it is descriptive, not load-bearing.

The AgenticOS workshop §Layer 6 names a third responsibility the v0.4 schema does not cover: **the agent as a recallable artifact**. An agent profile in the AgenticOS sense is durable context describing what an autonomous worker does — its purpose, the inputs it expects, the outputs it produces, the cadence at which it runs, the skills it depends on, the verification checklist that gates its delivery. That is recallable user context, not auth state. SWCR should bundle an agent profile with the related project facets when the user asks "what is the digest agent doing this week?"; the auth pipeline must not.

Two design failures are equally available and equally bad:

- **Collapse the two.** Stash agent profile fields onto `agents.metadata`. Every read against `agents` becomes a context fetch; every retrieval against an agent profile becomes a privileged-table read; the per-row scope check loses meaning because every connected tool sees every other tool's profile metadata.
- **Hide the agent profile inside `project`.** Append `[agent: digest_v3]` to a project facet. SWCR weights it against project register, so cross-references to the agent's verification checklist or its skill list collapse into prose. Recall returns a paragraph, not the structured object the AgenticOS workshop assumes.

The right answer is the same answer ADR-0010 used for `person` and `skill`: a new facet type with structured metadata, validated at the MCP/REST surface, scoped through the existing capability-token system. The new wrinkle is that an `agent_profile` facet must be **linkable** to an `agents` row so a token's calling tool can find its own profile, but the link must not collapse the two concepts into one row.

## Decision

Add `facet_type='agent_profile'` to the v0.5 facet vocabulary, with structured metadata and an additive FK linkage from `agents` to a profile facet:

### Facet shape

```
facet_type    : 'agent_profile'
external_id   : caller-supplied stable identifier (e.g. 'digest_v3')
content       : human-readable profile narrative (markdown)
metadata      : {
  purpose             : short string describing the agent's job
  inputs              : list of input descriptors (free-form strings or refs)
  outputs             : list of output descriptors
  cadence             : free-form schedule string ('weekly', 'on-demand', cron expression)
  skill_refs          : list of skill external_ids the agent depends on
  verification_ref    : optional external_id of a verification_checklist facet (ADR 0018)
}
```

`agent_profile` is `query_time` (per ADR 0010 — it is not synthesized at write time). Default `volatility='persistent'` (per ADR 0016 — agent profiles describe durable agents, not session-scoped scratch).

### `agents` table linkage

Add a nullable column to `agents`:

```sql
profile_facet_external_id TEXT NULL REFERENCES facets(external_id) DEFERRABLE
```

The FK is nullable because:

- Existing v3 vaults have rows in `agents` with no profile facet; nullable means migration is additive and lossless.
- Auth tokens for tools that have not registered a profile must continue to work. The token-lifecycle invariants (ADR 0007) do not gain a "profile required" precondition.
- Profile registration and token issuance are separate operations. `register_agent_profile` creates the facet then UPDATEs `agents.profile_facet_external_id`. The two writes share a transaction.

The reverse direction (`agent_profile` facet → `agents` row) is not materialized. The facet's `agent_id` (existing FK on every row in `facets`) already records which agent owns the profile.

### Three new MCP tools (REST parity)

| Tool                        | Scope                        | Behavior                                                       |
| --------------------------- | ---------------------------- | -------------------------------------------------------------- |
| `register_agent_profile`    | `write:agent_profile`        | Creates the facet + updates `agents.profile_facet_external_id` |
| `get_agent_profile`         | `read:agent_profile`         | Returns profile by `external_id` or `null`                     |
| `list_agent_profiles`       | `read:agent_profile`         | Returns active profiles                                        |

`agent_profile` is added to the auth scope allowlist. `recall(facet_types=all)` includes agent_profile facets when the token's read scope grants them; the token granted to a digest agent typically scopes itself out of other agents' profiles.

### Boundary statement

**`agents` is the JWT subject store. `agent_profile` is the recallable context. The two are linked, not merged.**

Implementation rules that fall out of this boundary:

- Auth code (`src/tessera/auth/`) reads only `agents`. It must not load profile metadata to make a token decision.
- Recall code (`src/tessera/retrieval/`) treats `agent_profile` like any other facet type. It must not special-case it through the `agents` table.
- Audit events for token issuance reference `agents.id`. Audit events for profile mutation reference `facets.id` of the profile row.
- The `agents` table never gains profile-shaped columns (`purpose`, `inputs`, etc.). If a fourth responsibility for `agents` ever surfaces, open a new ADR before extending the schema.

## Rationale

1. **Facet type, not table.** Profiles are recallable. Recallable context lives in `facets`. Adding a parallel `agent_profiles` table would force every retrieval surface to learn a second loader, every auth scope to gain a parallel allowlist entry, and every backup/export to know the new table — for no benefit.
2. **Structured metadata, not free-form `agents.metadata`.** Treating `agents.metadata` as the home for profile fields means the auth pipeline must read profile data to find the auth principal, and the retrieval pipeline must read auth data to find the profile. Separating them keeps each pipeline narrow.
3. **Nullable FK from `agents`.** Tokens predate profiles. A FK with NOT NULL would either invent an empty placeholder row at token issuance (semantically wrong — the agent has not registered a profile) or block token issuance until profile registration (operationally wrong — bootstrapping a new tool needs a token before the tool can write a profile). Nullable matches the lifecycle.
4. **Profile registration mutates `agents`, not the other way.** A profile facet may be replaced (the agent evolves; the user replaces the facet). The `agents` row stays. Token rotation does not orphan a profile.
5. **`recall` surfaces profiles uniformly.** When the user asks "what is the digest agent doing?", SWCR returns the profile bundled with related project / verification / skill facets. No separate `get_agent_status` API exists. The bundle answers the question.
6. **Verification linkage is optional, not required.** `verification_ref` is nullable. Agents without a verification checklist (low-stakes, exploratory) ship without one. ADR 0018 defines the checklist facet; ADR 0017 records only the linkage shape.

## Consequences

**Positive:**
- AgenticOS Layer 6 maps cleanly onto the existing facet vocabulary; no parallel agent system.
- The auth boundary is preserved. JWT subject lookups stay narrow; profile reads route through `recall` and its scope checks.
- Profile evolution is a normal facet mutation: replace, soft-delete, version through `external_id` updates, audit-log every change.
- Capability tokens can scope themselves out of other agents' profiles (`read:agent_profile` is grantable independently of `read:project`).

**Negative:**
- One extra facet type in scopes, allowlists, MCP/REST surfaces, doctor checks. Three new MCP tools. Modest surface expansion.
- Token issuance must remember to populate `agents.profile_facet_external_id` if the caller registered a profile in the same operation. The two-step pattern (create profile, then update `agents` row) is one transaction in the dispatcher; if the second step fails the transaction rolls back.
- Existing connectors that use `agents.metadata` ad hoc must migrate to a registered profile or accept that their description is invisible to recall. Connector authors get a documented migration path; no breaking change to existing tokens.

## Alternatives considered

- **Collapse: stash profile fields on `agents.metadata`.** Rejected. Auth and retrieval start sharing a table. Per-row scope checks lose granularity (every token sees every agent's profile). Profile evolution conflates with auth-row evolution.
- **Hide profile inside `project`.** Rejected. SWCR weights agent profile content as project register; cross-references to skills and verification facets collapse into prose; recall cannot return the structured object connectors expect.
- **Materialize the FK in both directions.** Rejected. The reverse direction is queryable through `facets WHERE agent_id = ? AND facet_type = 'agent_profile'`; a redundant table column would need to be kept in sync on every profile mutation.
- **Make `verification_ref` required.** Rejected. Forces every agent to ship with a checklist before it can register a profile. ADR 0018 positions checklists as elective; mandating them here creates a circular dependency between ADRs.
- **One MCP tool that covers register + get + list.** Rejected. The tool name documents the operation; collapsing them obscures the scope check (each scope is `write:agent_profile` vs. `read:agent_profile`, not `agent_profile`).

## Revisit triggers

- Real-user data shows tools register profiles every session, then forget. Either tighten the lifecycle to `volatility='session'` per agent, or document that registration is an idempotent operation.
- A second responsibility wants to attach to `agents` (binding fingerprint, encryption-key id, anything else). Open a new ADR; do not extend the boundary stated here.
- `verification_ref` becomes load-bearing for trust posture (e.g., post-V0.5-P3 dogfooding shows agents without checklists produce too many bad outputs). Re-evaluate whether the field should be required at registration time.
- Profile-registration churn produces too many soft-deleted facets in `audit_log`. Add an explicit `update_agent_profile` tool to mutate in-place rather than replacing.

## Related documents

- `docs/adr/0007-token-lifecycle.md` — capability token lifecycle, unchanged by this ADR.
- `docs/adr/0010-five-facet-user-context-model.md` — facet-type vocabulary; this ADR adds `agent_profile`.
- `docs/adr/0016-memory-volatility-model.md` — agent profiles default to `volatility='persistent'`.
- `docs/system-design.md §Trust & capability tokens` — auth boundary; the boundary statement above belongs here when the V0.5-P2 schema delta lands.
- `docs/threat-model.md` — auth pipeline surface; updated in V0.5-P2 to record that the new FK does not change S2 (capability tokens) threats.
- `docs/release-spec.md §v0.5` — DoD bullet for the new facet type and the three MCP tools.
- `docs/migration-contract.md` — the v3 → v4 migration step list adds the `agents.profile_facet_external_id` nullable FK column.
