# ADR 0023 — OKF interchange boundary

**Status:** Proposed
**Date:** June 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0010](0010-five-facet-user-context-model.md), [ADR 0016](0016-memory-volatility-model.md), [ADR 0019](0019-compiled-notebook-as-agenticos-playbook.md), [ADR 0020](0020-automation-registry-storage-only.md), [ADR 0021](0021-audit-chain-tamper-evidence.md), [ADR 0022](0022-byo-sync-transport.md), `.docs/okf-integration-enhancement-plan.md`, `docs/non-goals.md`, `docs/system-overview.md`, `docs/system-design.md`

## Context

Google Cloud published the Open Knowledge Format (OKF v0.1, "Draft") on 2026-06-13: a deliberately tiny interchange format — a directory of markdown files, each with YAML frontmatter, one "concept" per file, file-path as concept ID. Its only hard conformance rule (SPEC §9) is *parseable frontmatter with a non-empty `type` on every non-reserved `.md`*; everything else (`title`, `description`, `resource`, `tags`, `timestamp`, cross-links, `index.md`/`log.md`, `# Citations`) is soft, and consumers MUST tolerate unknown types, unknown keys, and broken links. Its lineage is Karpathy's LLM-wiki gist — the same lineage Tessera's `compiled_notebook` already cites (ADR 0019).

OKF overlaps almost perfectly with Tessera's disk-facing surfaces — skill disk-sync today (`vault/skills.py`) and the planned v0.6 project-context layer — and conflicts directly with the core: SQLCipher encryption-at-rest (moat #1), capability-token scopes, the tamper-evident audit chain (ADR 0021), and SWCR cross-facet retrieval. Plaintext-on-disk is the entire OKF value proposition; encrypted-at-rest is the entire Tessera trust posture. Those two cannot both hold inside the vault.

Without a decision on the record, "Tessera speaks OKF" is ambiguous and could drift three ways, each corrosive:

1. **OKF as native vault storage** — facets stored as plaintext markdown as the system of record. This breaks encryption, the audit chain, per-facet scopes, content-hash dedup, and SWCR's SQL-backed retrieval.
2. **OKF as a sync transport** — bundles used as the multi-device sync mechanism. This shadows the already-shipped encrypted BYO-sync (`sync/`, ADR 0022) with an unencrypted, unsigned, unaudited path — strictly weaker security.
3. **OKF as an explicit projection** — a decrypted bundle the user asks Tessera to emit, plus the on-disk shape for the v0.6 project-context layer. This is the only direction that keeps every core invariant intact.

A bundle is "just files" with no scopes and no audit, which makes options 1 and 2 perpetually tempting. This ADR pins the boundary before any OKF code lands so the temptation cannot be satisfied later by accident.

## Decision

**OKF is a boundary/interchange direction, not a core-architecture direction.** Tessera adopts OKF as an explicit, decrypted **projection** of the vault and as the on-disk **convention** for the v0.6 project-context layer. OKF is **never** native vault storage and **never** a sync transport.

### Boundary statement

> **Tessera stores encrypted; an OKF bundle is what the user explicitly asks Tessera to emit.**

This mirrors the boundary pattern established by the preceding ADRs:

- ADR 0019 — *Tessera stores synthesized artifacts; an out-of-process compiler produces them.*
- ADR 0020 — *Tessera registers automations as data; runners execute them.*
- ADR 0021 — *Tessera records operations; the audit chain makes tampering evident within a stated claim boundary.*

The encrypted vault remains the single source of truth for retrieval, capability checks, audit, and sync. An OKF bundle is a downstream artifact, produced only on demand, that carries none of those responsibilities.

### Trust slot

The OKF bundle sits in the **same trust slot** as the existing `tessera export --format md` / `--format sqlite` outputs: a decrypted, local, user-initiated projection. It introduces no new decryption primitive. It is never a default, never automatic, never a network sync. Producing one is an explicit, audited, user-initiated act — the same envelope as `export --format md`.

| Surface | Representation | Trust envelope | Mechanism |
| --- | --- | --- | --- |
| The vault | SQLCipher-encrypted SQLite | Encrypted at rest, scoped, audited | `vault/`, `auth/`, `audit_chain` |
| Backup / multi-device | Envelope-encrypted blobs + signed manifest | Ciphertext only; replay-defended | `sync/` (BYO sync — ADR 0022, **unchanged**) |
| **Interchange / portability** | **Conformant OKF bundle (plaintext markdown)** | **Explicit decrypted projection the user asks for** | **`export --format okf` / `import-okf` (forthcoming)** |
| Repo-local project context (v0.6) | OKF-shaped markdown sections on disk | Plaintext, git-reviewable, opt-in per repo | v0.6 adapter (adopts OKF conventions) |

### Identity across the boundary

OKF identity is path-based (a rename is a new identity); Tessera identity is the ULID `external_id` plus content-hash, which drive un-delete, dedup, and the audit chain's notion of identity. The ULID rides in a `tessera_external_id` frontmatter extension key so round-trips resolve on stable identity, not on a mutable path. Per SPEC §4.1, consumers MUST tolerate and round-trip unknown keys, so the `tessera_`-prefixed extension keys are conformant.

### Conformance posture

Conformance is an **output**, not a maintained contract. Tessera emits conformant OKF v0.1 and stays a documented superset; it does not pin itself to a single-vendor v0.1 draft spec's governance. The import write-path stays strict regardless of OKF's permissive consumer model: OKF `type` maps to an allowed `facet_type` or the concept is rejected; the allowlist, metadata caps, and path-traversal defenses are never relaxed to match OKF tolerance.

## Rationale

1. **The conflict is exactly at the encryption moat.** Every *strong* OKF↔Tessera fit is on a disk-facing surface Tessera already has or is about to build; every *conflict* is with the encrypted, scoped, audited core. The honest conclusion is "boundary, not core." Adopting OKF where Tessera meets the outside world keeps the moat; adopting it inside the vault destroys it.
2. **An explicit projection adds no new attack surface.** `export --format md` and `--format sqlite` already produce decrypted local copies. An OKF bundle is the same act in a different shape, so it inherits the existing trust slot rather than minting a new decryption path.
3. **The not-sync rejection protects ADR 0022.** A plaintext bundle has no scopes, no signatures, and no audit. Using it as multi-device sync would silently weaken the security model that the encrypted BYO-sync transport exists to provide. The two lanes stay clearly labeled: encrypted BYO-sync (transport) vs OKF-export (interchange).
4. **Pinning the boundary before code prevents creep.** Storage-creep and sync-creep are the predictable failure modes (a bundle looks like a tempting system-of-record or a tempting sync path). Recording the rejection now means any future proposal to make OKF native storage or a sync transport must open a new ADR and re-litigate this one, rather than slipping in through a feature request.
5. **ULID-in-frontmatter is the minimum that makes round-trip safe.** Without a stable identity key, export→import creates duplicates or desyncs identity against the audit chain. A single extension key resolves it while keeping the path display-only.
6. **The interop claim is sized honestly.** OKF's design center is data-catalog sharing (tables, metrics, BQ `resource`). Tessera facets are personal operating-model atoms with no `resource`. The value Tessera buys is "a *specified*, git-diffable markdown convention," not "plugs into Google's catalog tooling." `resource`-less personal facets are explicitly conformant (SPEC §4.4), and this ADR claims no ecosystem interop it will not get.

## Consequences

**Positive:**
- The encryption, sync, and audit invariants that *are* the product are protected in writing before any OKF code lands.
- A real portability/interop story becomes available as an additive, low-risk export — on-brand with "dotfiles for AI tools" — without advancing or endangering either moat.
- The v0.6 project-context disk layer can adopt an existing, specified convention instead of inventing a bespoke one, with interop for free.
- The "not a sync transport / not native storage" rejection is canonical and cited from `non-goals.md`, so the boundary survives future feature pressure.

**Negative:**
- A decrypted plaintext bundle is an exfiltration path for the most sensitive facets if mishandled (committed to a public repo, dropped in a synced folder). This ADR scopes OKF to explicit-export-only; the loud warning, optional scrub, and clobber guard that make the projection safe by construction are committed in the exporter/safety work (enhancement-plan Phase 3), not here.
- Tessera tracks a v0.1 draft spec. The superset/output-not-contract posture absorbs churn, but a major OKF rename (SPEC §11) would require a mapping update.
- Two clearly-labeled disk-facing lanes (encrypted BYO-sync vs plaintext OKF-export) are a concept users must keep straight; the docs must state the distinction plainly.

## Alternatives considered

- **OKF as native vault storage.** Rejected. Breaks SQLCipher encryption (moat #1), the audit chain, per-facet scopes, content-hash dedup, and SWCR's SQL-backed retrieval. Plaintext-vault-at-rest is an ideology bar in `docs/non-goals.md`.
- **OKF as a multi-device sync transport.** Rejected. Unencrypted, unsigned, unaudited; it shadows the encrypted BYO-sync (`sync/`, ADR 0022) with strictly weaker security. Recorded as a non-goal so the rejection is enforceable.
- **Auto-export / daemon-driven OKF emission.** Rejected. Turns an explicit decrypted projection into a silent exfiltration risk and violates the auto-capture/auto-act ideology bars. Export stays explicit and user-initiated.
- **Coercing unknown OKF `type` values into facets on import.** Rejected. Violates the strict write-path; unknown types are rejected for write, never silently coerced.
- **Reshaping SWCR/retrieval around OKF.** Rejected. OKF is orthogonal to retrieval; no benefit, large risk.
- **No ADR; let "Tessera speaks OKF" be defined in code.** Rejected. The storage and sync creep paths are exactly the kind of trade-off that gets relitigated without a written record; this ADR is the record.

## Revisit triggers

- A feature request proposes OKF as native storage or as a sync transport. Do not extend the boundary in code; open a follow-up ADR that re-litigates this one.
- OKF ships a major version with breaking frontmatter renames (SPEC §11). Update the mapping; keep the superset posture; do not hard-couple.
- Real-user signal shows the interop value is larger or smaller than claimed. Re-size the framing honestly; the boundary does not move with the size of the win.
- The v0.6 project-context layer begins. Fold the OKF conventions into its disk format per the enhancement plan's Phase 5; the encrypted vault stays the retrieval/auth/audit/sync source of truth.

## Related documents

- `.docs/okf-integration-enhancement-plan.md` — the phased plan this ADR opens; Phase 0 is this decision, Phase 6 finalizes the status to Accepted once the Approach-A export/import surface lands.
- `docs/non-goals.md` — carries the "OKF is not a sync mechanism" entry; canonical source for the not-sync/not-storage rejection.
- `docs/system-overview.md` — the "Interchange (OKF)" note states the boundary in product terms.
- `docs/system-design.md` — the "Interchange surface (OKF)" note states the boundary in architecture terms.
- `docs/adr/0019-compiled-notebook-as-agenticos-playbook.md` — Karpathy-wiki lineage; `# Citations` / provenance map to OKF citations.
- `docs/adr/0021-audit-chain-tamper-evidence.md` — an explicit export is an audited, user-initiated act.
- `docs/adr/0022-byo-sync-transport.md` — the encrypted transport OKF must never shadow.
