# Tessera — System Overview

> _The soul that outlives the substrate._

**Status:** Draft 1
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## What Tessera is

Tessera is a substrate-independent identity layer for AI agents. It stores who an agent _is_ — its memory, voice, learned skills, working relationships — in a single file on the user's machine, and serves that identity to any agent that asks for it via MCP. When the underlying model changes (Opus 4.6 → 4.7, GPT-5.3 → 5.4, cloud → local Qwen), the agent stays the same agent. Different body. Same soul.

Tessera is not a notes app. Not an agent runtime. Not a memory passport for chatbots. It is the layer that makes an agent _the same agent_ across every model swap, provider change, or substrate migration.

The lead user is the agent. The human is one of the agents.

## Why Tessera exists

Two trends are colliding in early 2026, and the collision creates a problem nobody's solving cleanly.

**Model versions ship faster than agents can stabilize.** The release window between Opus 4.6 and 4.7 was six weeks. GPT-5.3 to 5.4 was similar. Each version shifts capabilities, prompt sensitivities, tool-use defaults, judgment patterns. An agent built and tuned on one version often behaves materially differently on the next, even within the same provider's ecosystem. A common framing in the autonomous-agent community is "new body, same soul" — but in practice today, model swaps produce a new body and a _new_ soul. Every restart from a different substrate is a new agent wearing the old name.

**Autonomous agents are proliferating.** Claude Code, Codex, OpenClaw, Letta Code, Cline, custom harnesses — the population of long-running agents is growing fast. These agents accumulate state: conversations, decisions, learned patterns, relationships with users. Currently, that state is locked to the model and the harness. Change either, and the agent loses everything.

The pain isn't memory in the narrow sense ("I forgot a fact you told me"). The pain is identity rupture. There is currently no clean way for an agent to be _the same agent_ across substrate changes. Tessera makes the soul portable.

## How it works

A small daemon runs locally on the user's machine. It owns a single-file SQLite vault containing the agent's identity, segmented into facets — episodic memory, semantic memory, voice/style samples, learned skills, working relationships. The vault speaks MCP. Any MCP-capable agent can connect with a scoped capability token and read or write its identity. When the model behind the agent changes, the new substrate connects to the same vault, calls `assume_identity()`, and reconstructs the agent's continuity from the facets stored there.

Retrieval is built on the SWCR (Sequential Weighted Context Recall) framework — topology-aware multi-agent retrieval that returns coherent identity facets, not just nearest-neighbor matches. This is the technical depth that makes "identity reconstruction" actually work, rather than being a marketing line on top of vanilla cosine search.

The substrate is fungible. The vault is the agent. See the System Design document for full architecture.

## Market context

The "AI memory" category is crowded as of April 2026. Honest map:

### Memory layers (closest competitors)

| Product                         | Position                | Local                 | License          | Notes                                                                             |
| ------------------------------- | ----------------------- | --------------------- | ---------------- | --------------------------------------------------------------------------------- |
| **Mem0 / OpenMemory MCP**       | Memory layer + dev SaaS | Yes (Docker stack)    | Apache 2.0       | $24M Series A, AWS partnership, 14M downloads, Docker + Postgres + Qdrant install |
| **MemPalace**                   | Local memory layer      | Yes (SQLite + Chroma) | MIT              | 96.6% on LongMemEval, 19 MCP tools, no API keys                                   |
| **CaviraOSS/OpenMemory**        | Memory layer            | Yes (npm)             | Apache 2.0       | Native MCP, temporal knowledge graph                                              |
| **doobidoo/mcp-memory-service** | Memory layer            | Yes                   | OSS              | Production-tested, OAuth, 1500+ tests                                             |
| **Cognee**                      | Memory + graph          | Self-host or cloud    | Open core        | 30+ data connectors, enterprise-priced                                            |
| **Zep / Graphiti**              | Temporal memory         | Self-host or cloud    | Source-available | Strong on temporal queries                                                        |
| **Memori / Memorilabs**         | SQL-native memory       | Self-host             | Open core        | Treats memory as relational data                                                  |
| **OB1 / Open Brain**            | Memory layer            | Yes (Supabase)        | FSL-1.1-MIT      | Distribution-led, content-creator-built                                           |

### Agent runtimes (different category, often confused)

| Product                       | Position                            | Notes                                                     |
| ----------------------------- | ----------------------------------- | --------------------------------------------------------- |
| **Letta / Letta Code**        | Agent runtime with memory built in  | Replaces Claude Code; not a memory layer for other agents |
| **Claude Code (with memory)** | Anthropic's agent + provider memory | Locked to Anthropic ecosystem                             |

### Provider-level memory (the platform threat)

| Product                          | Position                                   | Notes                                          |
| -------------------------------- | ------------------------------------------ | ---------------------------------------------- |
| **ChatGPT Memory**               | Cross-conversation memory inside OpenAI    | References all past chats automatically        |
| **Google Personal Intelligence** | Gemini + Gmail + Photos + YouTube + Search | Launched January 2026, Google-ecosystem-locked |
| **Claude Projects memory**       | Project-scoped memory                      | Stays inside the project boundary              |

### Browser-extension memory (consumer flank)

| Product                     | Position                  | Notes                                                  |
| --------------------------- | ------------------------- | ------------------------------------------------------ |
| AI Context Flow             | Cross-tool browser plugin | Works across ChatGPT, Claude, Gemini, Perplexity, Grok |
| MemSync                     | Cross-app memory          | Semantic + episodic                                    |
| myNeutron                   | Chrome capture            | Captures online activity                               |
| OpenMemory Chrome Extension | Browser-only cross-tool   | Different project, same name                           |

The category is saturated for _fact recall_. Every product above frames the value prop as "remember things you said." None frame it as **agent identity continuity across substrate changes.** That's the positioning hole.

## Where Tessera fits

Tessera does not compete with Mem0 on fact recall. Mem0 has a $24M war chest, an AWS partnership, and 14M downloads. Tessera does not compete with Letta on agent execution. Letta is a Claude Code competitor with persistent agents.

Tessera competes on a frame nobody else has staked out: **the substrate-independent self.** What persists when you swap GPT for Claude for Llama for whatever ships next month.

The closest analogues are not memory products at all:

- **dotfiles** for shell environments — the layer that makes any shell feel like _your_ shell
- **password managers** for credentials — the layer that makes any browser feel like _your_ browser
- **Apple Continuity** for devices — the layer that makes any device feel like _your_ device

Tessera is "Continuity for AI agents." The category is not yet occupied by a funded entrant. It is claimable, not claimed. That distinction matters for the moat discussion below.

### Audience reality check

The addressable audience in April 2026 is narrow. Two populations feel substrate-change pain:

- **Developers running custom or long-running autonomous agents** — Claude Code power users, Codex, OpenClaw, Cline, Letta, custom harnesses. Honest global count: **500–5,000**.
- **Teams running self-hosted agent deployments who migrate between models for cost or capability reasons** — niche today, likely growing. Honest count: **low hundreds of teams**.

Everyone else — the mainstream user of ChatGPT, Gemini, or Claude via provider-native chat — does not feel this pain. Provider memory solves their use case adequately and keeps them on one substrate.

Tessera is not a mass-market product. It is a tool for power users and autonomous-agent operators. The v1.0 ambition is 100+ active vaults in the wild. A category that grows to 50K users over the next three years is a win; staying at 5K would make Tessera a craft project that served its niche well. Both outcomes are acceptable.

## Moat (in order of defensibility)

1. **The framing itself — conditional on speed.** "Agent identity" as a category is not yet claimed by any well-funded competitor. Naming a category is a real moat when the name sticks before a funded entrant co-opts it; Linear and Stripe are the positive examples. The negative examples (categories named first but owned by a later entrant with better distribution) are more common. A Medium post and a docs repositioning by Mem0 or Letta could occupy this frame in 48 hours. Tessera's defense is speed to public claim plus SWCR depth plus single-binary install — not framing alone. Without the technical moat below, the narrative moat is 6–12 months, not durable.

2. **SWCR-based retrieval.** Topology-aware multi-agent retrieval is genuinely deeper than vanilla vector search or single-pass rerank. SWCR is unpublished dissertation research applied directly to a product surface. Mem0 cannot copy it without rebuilding their hybrid datastore pipeline; OpenMemory cannot copy it without abandoning Qdrant; MemPalace cannot copy it without rewriting their compression scheme. This isn't a small architectural difference — it's a different retrieval philosophy.

3. **Single-binary install.** Every direct competitor ships Docker (OpenMemory, doobidoo), npm (CaviraOSS), or a CLI requiring a runtime (Letta Code). A genuinely single-binary install — `brew install tessera`, no Docker, no Postgres, no Qdrant — is a real friction asymmetry. The mainstream "true non-technical user" is gated by setup friction; first-mover on zero-friction install matters.

4. **All-local default.** Default to Ollama for embedding, extraction, and reranking. Cloud providers are opt-in, never required. Most competitors default to OpenAI keys. The DX-pain narrative around model providers is growing; aligning with the open-source-LLM movement is a structural bet, not just a feature.

5. **Aesthetic and UX discipline.** Most memory products are dev-tools-ugly. A genuinely opinionated, prosumer-quality interface (CLI first, optional GUI later) that does not compete on features but on shape — Linear vs. Jira, Bear vs. Evernote — is a moat that compounds with brand over time.

What is _not_ a moat: Apache 2.0 license (everyone has it), MCP support (everyone has it), local storage (everyone has it), graph layer (Mem0g, Cognee, Letta Filesystem all have it). Stating these as differentiators would be self-deception.

## Risks (stress-tested)

### Risk 1 — Provider memory closes the gap on the human use case

**Scenario.** ChatGPT Memory, Gemini Personal Intelligence, and Claude Projects all ship v3-grade cross-conversation memory. Users settle into one provider, never feel the cross-tool friction.

**Realism.** High. ChatGPT now references all past conversations across the platform. Google Personal Intelligence connects Gemini to Gmail, Photos, YouTube, and Search.

**Defense.** This kills "human PKM cross-tool memory" as a wedge — but Tessera isn't selling that. The agent-identity frame is orthogonal: provider memory does not solve substrate-change identity continuity, because providers want you locked to their substrate. The more they push their own memory, the more they validate that identity-substrate separation is a real category — and the more locked-in their memory becomes, the more painful it is to leave.

### Risk 2 — A funded entrant pivots into the agent-identity frame

**Scenario.** Mem0 or Letta repositions as "agent identity layer." They have funding, brand, and audience.

**Realism.** Medium-high. Mem0 and Letta can reposition faster than Tessera can build. A blog post and a docs update are the cost of co-opting the frame. Neither would have to abandon their existing product; both could add "identity layer" language on top of their current stack within a week.

**Defense.** The durable defenses are not narrative. They are:

- **SWCR retrieval depth** — published algorithm, ablation evidence, measurable advantage over RRF+Cohere rerank. Copying this requires rebuilding the retrieval pipeline. See `docs/swcr-spec.md` and `docs/benchmarks/B-RET-1`.
- **Single-binary install on the all-local default path** — copying this means replacing a Docker + Postgres + Qdrant stack. An engineering-months commitment for Mem0; OOS for Letta, which is structurally a runtime.
- **Encryption-at-rest by default and local-first posture** — small surface, consistent ideology.
- **Speed to public claim** — matters for SEO and community identity, but is the weakest of the four. A funded incumbent with better distribution can outrun this.

Tessera's case for the agent-identity category rests on the first three. If SWCR does not clear its ablation bar, the case collapses to "simpler packaging" — defensible but not a category claim.

### Risk 3 — Platform-level identity ships at the OS layer

**Scenario.** Apple, Google, or Microsoft ships a system-wide AI identity layer. Any AI app can read/write a shared user context.

**Realism.** Medium for partial in 18–24 months, high for full in 3–5 years. Apple specifically is positioned for this.

**Defense.** Cross-platform portability. Apple's version will be Apple-only by definition. Tessera explicitly works across macOS, Linux, Windows, and (eventually) Android. The cross-substrate, cross-OS, cross-provider story is exactly what platform-level identity _cannot_ offer without breaking its own walled garden. Don't overbuild for this risk today; monitor and stay nimble.

## Examples

### Example 1 — Personal coding agent across model versions

A solo developer runs a custom coding harness backed by Claude Sonnet 4.5. The harness has Tessera connected. Over six months, the agent learns the developer's coding conventions (no-magic philosophy, type hints everywhere, Pydantic over dataclasses), accumulates project-specific context (archex codebase patterns), and develops a working voice in commit messages and PR descriptions.

Sonnet 4.7 ships. The developer swaps the model. The new substrate connects to the same Tessera vault, calls `assume_identity()`, and behaves continuously: same coding conventions, same project knowledge, same commit-message voice. No re-teaching. No tone drift. The substrate changed; the agent did not.

### Example 2 — Multi-agent knowledge handoff for research

A graduate student runs three agents: a literature-review agent (in Cursor), a writing agent (in Claude Desktop), and a citation-cleaning agent (custom Python harness). Each has its own Tessera identity, but they share a common namespace for research-specific facts (papers read, key claims, contradiction patterns).

The writing agent calls `recall("the contradiction we found between Smith 2024 and Patel 2025")`. The fact was originally captured by the literature-review agent. Tessera returns it with provenance: "captured by lit-review-agent, 2026-03-14, while reading Smith et al." The student doesn't have to broker the handoff manually.

### Example 3 — Long-running autonomous agent surviving infrastructure migration

A small business runs an autonomous customer-support agent on a self-hosted Letta deployment, backed by GPT-5.3. After three months, the agent has accumulated thousands of customer interactions, learned how to handle edge cases, and built a working model of recurring customer types.

The business migrates from GPT-5.3 to local Qwen 3 for cost reasons. Without Tessera, the agent loses everything — three months of learned behavior gone. With Tessera, the agent's identity (customer profiles, learned response patterns, escalation rules) lives in the vault, independent of the model. The new substrate picks up where the old one left off.

## Origin and posture

Tessera is a solo-developer craft project. Built by Tom Mathews, drawing on existing open-source primitives in the [`determ-ai`](https://github.com/determ-ai) and [`Mathews-Tom`](https://github.com/Mathews-Tom) ecosystems — specifically SWCR (retrieval), archex (graph), mudra (dedup), and the no-magic philosophy throughout.

This is not a venture-scale opportunity. It is a craft project that may grow if it earns an audience, with no expectation that it must. The roadmap is paced by what Tom can build solo, in evenings and weekends, while writing a dissertation on agentic memory systems.

Apache 2.0. No CLA. No telemetry. No hosted tier in v0.1. No model reselling. No paid features in the open-source core, ever.

If a real audience forms, the long-term monetization shape would be optional managed sync (BYO storage is always free) — the Obsidian Sync playbook. That's a years-out concern, and not the reason this exists.

The reason this exists is that the substrate-change problem is real, the engineering shape is interesting, and the existing products in the space all miss the framing.

---

## Reading next

- **System Design** — full architecture, schema, retrieval pipeline, MCP surface
- **Pitch** — share with colleagues to test the waters
- **Release Spec** — what ships in v0.1, v0.3, v0.5, v1.0
