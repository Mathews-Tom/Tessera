# Tessera — Non-Goals

**Status:** Draft 1
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

This document is the canonical source of everything Tessera will not do. The list is consolidated from references scattered across `pitch.md`, `system-design.md`, and `release-spec.md`. Where those documents disagree with this one, this one wins.

Non-goals come in three categories:

1. **Deferred** — not in this version, but may ship later. See version roadmap for targets.
2. **Ideology bars** — will never ship in the open-source project regardless of demand.
3. **Out of product scope** — different product, different project, sometimes complementary.

## Deferred (by version)

| Item                                                    | Not in            | Earliest        | Rationale                                                                                                                                  |
| ------------------------------------------------------- | ----------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `person` facet                                          | v0.1              | v0.3            | Relationship modelling benefits from real multi-month usage; ships only if v0.1 signal calls for it (per ADR 0010)                         |
| `skill` facet                                           | v0.1              | v0.3            | Markdown-file-vs-vault-row trade-off benefits from real usage (per ADR 0010)                                                               |
| Importers (ChatGPT, Claude export, Obsidian, email)     | v0.1              | v0.3            | Each importer is a small project; ship v0.1 first. Importers backfill the five v0.1 facets, not `skill` (skills are user-authored)         |
| Light entity resolution                                 | v0.1              | v0.3            | Entities live in metadata JSON in v0.1; structured resolution lands with `person_mentions` at v0.3                                         |
| Episodic temporal queries                               | v0.1 / v0.3       | v0.5            | Not a first-class facet type post-reframe (per ADR 0010); if v0.5 user signal calls for time-aware retrieval on projects, revisit          |
| `compiled_notebook` facet + write-time compilation      | v0.1 / v0.3       | v0.5            | Vertical-depth synthesis; needs v0.1 users to shape the compiler                                                                           |
| BYO cloud sync                                          | v0.1 / v0.3       | v0.5            | Architecturally simple; adds surface area                                                                                                  |
| Per-facet mode toggle (Framing Y) on existing types     | v0.1 through v0.5 | post-v0.5       | `compiled_notebook` carries write-time; existing types stay `query_time`. A user-visible per-facet mode toggle ships only if post-v0.5 signal calls for it |
| Repo-local project-context graph                        | v0.1 through v0.5 | v0.6 candidate  | Useful as a markdown authoring/checking adapter over facets after v0.5; not a replacement for the encrypted vault                         |
| Multi-user vaults / shared namespaces                   | v0.1 through v0.5 | v1.0 or later   | Requires mature permission model                                                                                                           |
| Optional desktop GUI                                    | v0.1 through v0.5 | v1.0 (optional) | CLI remains primary; GUI is opt-in, feature-parity for read operations only                                                                |
| Optional hosted sync                                    | v0.1 through v0.5 | v1.0 (optional) | Solo-dev cannot operate user-facing infrastructure pre-v1.0; BYO S3 always free                                                            |
| Token binding (UID, client fingerprint)                 | v0.1              | v0.3            | Opt-in hardening in v0.3; mandatory for service tokens under v1.0 consideration                                                            |
| Audit-log HMAC chain                                    | v0.1              | v0.3            | Operational need in v0.1 is convention-based; cryptographic tamper-evidence is v0.3                                                        |
| In-process Python plugins                               | v0.1 through v1.0 | None planned    | Extensibility is delivered through hooks, the REST surface at `/api/v1/*`, and the MCP surface at `/mcp`. Tessera does not expose a Python extension API; third-party code does not import `tessera.*` to extend the daemon. Connectors and importers under `src/tessera/connectors/` and `src/tessera/importers/` are first-party only. |

## Ideology bars — will never ship

These are not engineering deferrals. They are product commitments. Breaking one of them changes what Tessera is.

| Will not ship                                                                     | Why                                                                                                                                    |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Auto-capture (clipboard monitoring, screen recording, keylogging)                 | Surveillance is anti-ideology. The agent or user decides what to capture; the daemon stores. Separation of concerns is non-negotiable. |
| AI-generated capture (the daemon deciding what to remember on the user's or tool's behalf) | Same as above. The user or the connected AI tool decides what to capture. The daemon stores and retrieves; it does not editorialize.   |
| Hosted-only mode (no local option)                                                | Local-first is the foundation. Hosted is opt-in convenience if it ships at all.                                                        |
| Model reselling (premium tiers with bundled GPT-X access)                         | Tessera is the layer, not a model vendor. Reselling introduces provider conflict.                                                      |
| Proprietary embedding scheme                                                      | Lock-in destroys the portability claim.                                                                                                |
| Closed-source server with open-source client                                      | Apache 2.0 is the whole stack. No open-core sleight of hand.                                                                           |
| Telemetry or usage analytics in the open-source build                             | Verified by CI network-block test and grep check. Non-negotiable.                                                                      |
| Plugin marketplace with revenue share                                             | Not a v1.0 problem. Not a v3.0 problem. Maybe in 5 years if a community exists to justify it.                                          |
| Vendor-specific integrations (e.g., "The Official Anthropic Memory Layer")        | Tessera dies the day it becomes a vendor's official anything.                                                                          |
| Last-writer-wins conflict resolution for facets                                   | Silently drops learnings. Category-inconsistent with a portable-context layer. Append-on-conflict is the correct default.              |
| Plaintext vault at rest                                                           | Contradicts the user-owned-context framing. Encryption-at-rest is v0.1 mandatory.                                                      |
| URL-embedded token transport as a recommended configuration                       | Antipattern. ChatGPT Dev Mode connects via an exchange endpoint; URL tokens are deprecated on arrival.                                 |
| Markdown files as the canonical memory store                                      | Repo-local markdown may become an authoring/review adapter, but the vault remains canonical for auth, audit, retrieval, and sync.      |

## Out of product scope

These are different products. Tessera does not try to be them and does not compete with them.

| Product                                                                                | Why it's not Tessera                                                                                                                         |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent runtime (Letta, Claude Code, Cline)                                              | Tessera is the portable-context layer, not the execution layer. Any runtime or AI tool can consume Tessera.                                  |
| Note-taking app (Obsidian, Notion, Bear)                                               | Tessera is read/written by AI tools via MCP, not by humans composing notes. An Obsidian importer is a v0.3 feature; an Obsidian competitor is not a goal. |
| Markdown codebase graph tools                                                          | Tessera can learn from linked sections, source backlinks, and checks, but those remain adapter/workflow features over facets rather than a separate product. |
| Provider-native memory (ChatGPT Memory, Gemini Personal Intelligence, Claude Projects) | Those are single-provider lock-ins. Tessera is the inverse — portable across every MCP-speaking tool.                                        |
| Observability platform (Datadog, Honeycomb)                                            | Tessera is not monitoring AI tools. It is the user context they read and write.                                                              |
| Vector database (Pinecone, Weaviate, Qdrant)                                           | Tessera uses sqlite-vec internally. It is not a vector-database product.                                                                     |
| Conversational UI (chat frontends)                                                     | Tessera has no UI in v0.1 and an optional CLI-parity GUI only at v1.0.                                                                       |

## Commitment posture on non-goals

- **Deferred items** may move between versions. The roadmap is subject to real-user signal and solo-dev capacity.
- **Ideology bars** move only via explicit public RFC with a strong case for why the bar was wrong. To date, none of them is expected to move.
- **Out-of-scope items** may become plugins or external integrations; they do not become core features.

## Single source of truth

When a non-goal is cited in pitch, system-design, or release-spec, it links to this document. Edits to non-goal language happen here. The downstream documents quote, they do not redefine.
