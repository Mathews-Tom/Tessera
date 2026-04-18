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

| Item                                                   | Not in            | Earliest        | Rationale                                                                                        |
| ------------------------------------------------------ | ----------------- | --------------- | ------------------------------------------------------------------------------------------------ |
| Skills as first-class facet                            | v0.1              | v0.3            | Need real-user signal to inform format; markdown-file-vs-vault-row trade-off benefits from usage |
| Importers (ChatGPT, Claude export, Obsidian, X, email) | v0.1              | v0.3            | Each importer is a small project; ship v0.1 first                                                |
| Light entity resolution                                | v0.1              | v0.3            | Entities live in metadata JSON in v0.1; structured resolution is v0.3                            |
| Episodic temporal queries                              | v0.1 / v0.3       | v0.5            | Episodic storage exists; time-aware retrieval is a separate design                               |
| BYO cloud sync                                         | v0.1 / v0.3       | v0.5            | Architecturally simple; adds surface area                                                        |
| Relationships as a facet                               | v0.1 / v0.3       | v0.5            | Benefits from real multi-month usage to inform the model                                         |
| Goals as a facet                                       | v0.1 / v0.3       | v0.5            | Same as above                                                                                    |
| Judgment as a facet                                    | v0.1 through v0.5 | v1.0            | Multi-agent trade-off capture is v1.0                                                            |
| Multi-agent shared namespaces                          | v0.1 through v0.5 | v1.0            | Requires mature permission model                                                                 |
| Optional desktop GUI                                   | v0.1 through v0.5 | v1.0 (optional) | CLI remains primary; GUI is opt-in, feature-parity for read operations only                      |
| Optional hosted sync                                   | v0.1 through v0.5 | v1.0 (optional) | Solo-dev cannot operate user-facing infrastructure pre-v1.0; BYO S3 always free                  |
| Token binding (UID, client fingerprint)                | v0.1              | v0.3            | Opt-in hardening in v0.3; mandatory for service tokens under v1.0 consideration                  |
| Audit-log HMAC chain                                   | v0.1              | v0.3            | Operational need in v0.1 is convention-based; cryptographic tamper-evidence is v0.3              |

## Ideology bars — will never ship

These are not engineering deferrals. They are product commitments. Breaking one of them changes what Tessera is.

| Will not ship                                                                     | Why                                                                                                                                    |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Auto-capture (clipboard monitoring, screen recording, keylogging)                 | Surveillance is anti-ideology. The agent or user decides what to capture; the daemon stores. Separation of concerns is non-negotiable. |
| AI-generated capture (the daemon deciding what to remember on the agent's behalf) | Same as above. The agent is the lead user. The daemon does not editorialize.                                                           |
| Hosted-only mode (no local option)                                                | Local-first is the foundation. Hosted is opt-in convenience if it ships at all.                                                        |
| Model reselling (premium tiers with bundled GPT-X access)                         | Tessera is the layer, not a model vendor. Reselling introduces provider conflict.                                                      |
| Proprietary embedding scheme                                                      | Lock-in destroys the portability claim.                                                                                                |
| Closed-source server with open-source client                                      | Apache 2.0 is the whole stack. No open-core sleight of hand.                                                                           |
| Telemetry or usage analytics in the open-source build                             | Verified by CI network-block test and grep check. Non-negotiable.                                                                      |
| Plugin marketplace with revenue share                                             | Not a v1.0 problem. Not a v3.0 problem. Maybe in 5 years if a community exists to justify it.                                          |
| Vendor-specific integrations (e.g., "The Official Anthropic Memory Layer")        | Tessera dies the day it becomes a vendor's official anything.                                                                          |
| Last-writer-wins conflict resolution for facets                                   | Silently drops learnings. Category-inconsistent with an identity layer. Append-on-conflict is the correct default.                     |
| Plaintext vault at rest                                                           | Contradicts sovereign-identity framing. Encryption-at-rest is v0.1 mandatory.                                                          |
| URL-embedded token transport as a recommended configuration                       | Antipattern. ChatGPT Dev Mode connects via an exchange endpoint; URL tokens are deprecated on arrival.                                 |

## Out of product scope

These are different products. Tessera does not try to be them and does not compete with them.

| Product                                                                                | Why it's not Tessera                                                                                                                         |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent runtime (Letta, Claude Code, Cline)                                              | Tessera is the identity layer, not the execution layer. Any runtime can consume Tessera.                                                     |
| Note-taking app (Obsidian, Notion, Bear)                                               | Tessera is read/written by agents, not humans-composing-notes. An Obsidian importer is a v0.3 feature; an Obsidian competitor is not a goal. |
| Provider-native memory (ChatGPT Memory, Gemini Personal Intelligence, Claude Projects) | Those are single-provider lock-ins. Tessera is the inverse.                                                                                  |
| Observability platform (Datadog, Honeycomb)                                            | Tessera is not monitoring autonomous agents. It is the state they carry.                                                                     |
| Vector database (Pinecone, Weaviate, Qdrant)                                           | Tessera uses sqlite-vec internally. It is not a vector-database product.                                                                     |
| Conversational UI (chat frontends)                                                     | Tessera has no UI in v0.1 and an optional CLI-parity GUI only at v1.0.                                                                       |

## Commitment posture on non-goals

- **Deferred items** may move between versions. The roadmap is subject to real-user signal and solo-dev capacity.
- **Ideology bars** move only via explicit public RFC with a strong case for why the bar was wrong. To date, none of them is expected to move.
- **Out-of-scope items** may become plugins or external integrations; they do not become core features.

## Single source of truth

When a non-goal is cited in pitch, system-design, or release-spec, it links to this document. Edits to non-goal language happen here. The downstream documents quote, they do not redefine.
