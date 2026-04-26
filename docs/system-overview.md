# Tessera — System Overview

> *Portable context that travels with you across every AI tool.*

**Status:** Draft (post-reframe)
**Date:** April 2026
**Owner:** Tom Mathews
**License:** Apache 2.0

---

## What Tessera is

A portable context layer for AI tools. A local daemon (`tesserad`) owns a single-file SQLite vault holding the user's context across five facets — identity, preferences, workflows, projects, and style. Any MCP-capable AI tool reads and writes that context with a scoped capability token. The user teaches each thing once; every tool uses it thereafter.

Tessera is not a notes app, not an agent runtime, not a memory passport for chatbots. It is the *rules-of-engagement* layer between the user and every AI tool they use.

## Why Tessera exists — the pain

Three observations about how people actually use AI tools in 2026:

**1. Every user accumulates a personal operating model for AI.** Preferences (`uv` over `pip`, Pydantic over dataclasses), workflows (LinkedIn 5-act structure, long-form w→x→y→z), stylistic rules (no emojis in professional posts, terse for Reddit), project context (currently working on anneal), identity facts (AI-native backend engineer, DBA candidate). This operating model is real and stable.

**2. The operating model lives in a different place in every tool.** CLAUDE.md for Claude Code. Custom instructions for ChatGPT. Cursor rules for Cursor. Codex config for Codex. When any of these gets touched up, the others drift. The operating model is fragmented across tools and re-typed constantly.

**3. Each provider wants to own the memory layer.** ChatGPT Memory now references all past conversations. Gemini Personal Intelligence connects to Gmail, Photos, YouTube, Search. Claude Projects scope context per project. The providers' answer is: stay inside our ecosystem and we'll remember you. The implicit cost is lock-in to their substrate.

The T-shaped user — deep in one or two domains, active across many through AI — pays this tax hardest because they touch the most tools. The amnesia tax compounds: multiple tools × multiple contexts × new model versions every six weeks.

Tessera makes the operating model a first-class portable thing, owned by the user, readable by every tool.

## The T-shape as the unit of analysis

The single most important framing choice in Tessera's design is that **context is not flat**. A T-shaped user has two distinct kinds of context:

- **Vertical (deep):** the user's primary domains of expertise. For the archetypal user: backend systems, AI research. Stable over years. Evolves through deep engagement. Outputs are often long-form and synthesized.
- **Horizontal (broad):** everything else the user touches with AI assistance. Front-end decisions, database choices for a product, blog writing, social posts, mentoring conversations. Stable over weeks to months. Evolves through many small interactions. Outputs are short-form and transactional.

Almost every real AI interaction crosses both arms of the T. Drafting a LinkedIn post about a backend project: the *substance* is vertical (backend, AI research), the *form* is horizontal (LinkedIn voice, 5-act structure, no-emoji preference). Writing a Reddit comment about a new AI paper: vertical substance, horizontal register.

This has a concrete architectural implication: retrieval must be **cross-facet by default**. A query that hits only one facet is the exception, not the rule. This is why Tessera invests in SWCR (topology-aware retrieval) from v0.1 instead of shipping a flat cosine search.

## The write-time / query-time frame

Every AI knowledge system has to answer one question: when does the AI do the hard thinking? Write-time (compile on ingest, like Karpathy's personal-wiki prompt) or query-time (store cheaply, synthesize when asked, which is what most memory products do today).

Tessera ships v0.1 as **query-time only**. The reason is matched to the audience:

| Facet | Natural mode | Why |
|---|---|---|
| Identity | Query-time | Stable facts; synthesis at query is cheap and fresh |
| Preferences | Query-time | Rules-of-engagement; you want them applied, not compiled |
| Workflows | Query-time | Procedural patterns; reading them back is the whole point |
| Projects | Query-time | Active context; write-time compilation would stale fast |
| Style | Query-time | Voice samples; retrieval returns representatives, not synthesis |

Each of these facets is about the **horizontal touch of the T-shape**. Query-time is the right mode. Write-time compilation would add complexity without adding value for these types.

**Write-time compilation becomes relevant for the vertical depth of the T-shape** — long-running research notebooks, evolving domain knowledge. v0.5 adds `compiled_notebook` as a new facet type: the user tags a `project` or `skill` as vertical-depth, and a compilation agent synthesizes an artifact from those source facets. The v0.1 schema reserves the facet type and the `compiled_artifacts` table, so the transition is additive, not a rewrite.

The `mode` column on `facets` discriminates rows by **production method**, not user choice: v0.1 writes `query_time` for all five facets; v0.5 writes `write_time` for `compiled_notebook` rows produced by the compiler. A per-facet mode toggle on existing facet types is not a v0.5 commitment — if real user signal calls for it after v0.5, it's a later decision.

## Market context

Honest map as of April 2026.

### Memory layers (closest competitors)

| Product | Position | Local | License | Notes |
|---|---|---|---|---|
| **Mem0 / OpenMemory MCP** (Mem0's open-source release) | Memory layer + dev SaaS | Yes (Docker stack) | Apache 2.0 | $24M Series A, AWS partnership, 14M downloads |
| **MemPalace** | Local memory layer | Yes (SQLite + Chroma) | MIT | 96.6% on LongMemEval, 19 MCP tools, no API keys |
| **CaviraOSS/OpenMemory** (unrelated to Mem0's OpenMemory — naming collision) | Memory layer | Yes (npm) | Apache 2.0 | Native MCP, temporal knowledge graph |
| **doobidoo/mcp-memory-service** | Memory layer | Yes | OSS | Production-tested, OAuth, deployment-hardened |
| **Cognee** | Memory + graph | Self-host or cloud | Open core | 30+ data connectors, enterprise-priced |
| **Zep / Graphiti** | Temporal memory | Self-host or cloud | Source-available | Strong on temporal queries |
| **Memori / Memorilabs** | SQL-native memory | Self-host | Open core | Treats memory as relational data |
| **Cloud-Postgres "second brain" products** (emerging class) | Cross-tool memory served from a rented cloud database | Cloud-only | Varies | Content-creator-led distribution; architecturally welded to their cloud-PaaS backend |

### Provider-level memory (platform threat)

| Product | Position | Notes |
|---|---|---|
| ChatGPT Memory | Cross-conversation, OpenAI-locked | References all past chats automatically |
| Gemini Personal Intelligence | Google-ecosystem | Launched January 2026 |
| Claude Projects memory | Anthropic-scoped | Stays inside project boundary |

### Per-tool preference files (where the user currently lives)

| Product | Scope | Notes |
|---|---|---|
| CLAUDE.md | Claude Code, Claude Desktop | Markdown file, Claude-only |
| ChatGPT Custom Instructions | ChatGPT | Web UI only, ChatGPT-only |
| Cursor Rules | Cursor | Project or global, Cursor-only |
| Codex config | Codex CLI | Codex-only |
| Windsurf rules | Windsurf | Windsurf-only |

### Knowledge compilation (different category — write-time)

| Approach | Notes |
|---|---|
| Karpathy's personal wiki prompt | Write-time compiled Markdown in Obsidian; solo deep research |
| NotebookLM | Google's hosted variant; write-time synthesis per-notebook |
| Cloud-memory products announcing compilation extensions | Add a derived wiki layer over their existing cloud-Postgres backend |

## Where Tessera fits

Tessera does not compete with Mem0 on scale of memory infra. Mem0 has $24M and an AWS partnership. We do not compete with Karpathy's wiki on deep research compilation — v0.1 doesn't do write-time. We do not compete with CLAUDE.md on the Claude-specific experience — Claude's integration inside Claude is always going to be tighter.

Tessera occupies a position nobody has clearly claimed: **the cross-tool, structured, user-owned context layer for the T-shaped AI-native user.**

The closest stated analogue is **dotfiles for AI tools**. Your shell doesn't care which machine you SSH into; your `.zshrc` makes any shell feel like *your* shell. Tessera does the same for AI tools: your agent doesn't care which model or which harness; your context makes any AI feel like *your* AI.

## Moat

Ranked by defensibility. What's genuinely Tessera's, not marketing copy on top of shared primitives:

**1. Storage sovereignty.** The vault is a single SQLite file on the user's disk. No cloud Postgres, no rented database, no vendor account required to read your own context. Every cloud-dependent memory layer — whether it's Mem0's SaaS tier or a cloud-Postgres "second brain" product — forecloses offline use, single-file export, region-independence, zero-account install, and long-term independence from their infrastructure. That asymmetry is permanent: a cloud-memory product cannot adopt file-on-disk storage without abandoning its architecture.

**2. SWCR-based cross-facet retrieval.** T-shape synthesis — pulling style + project + workflow + preference coherently for a single query — is not what vanilla vector search does well. SWCR is unpublished dissertation research applied as the default retrieval mode, not an advanced option. Competitors would have to rewrite their retrieval stacks to match. Mem0 cannot, OpenMemory cannot, MemPalace cannot. This is the one technical moat that compounds with dissertation research Tom is doing anyway.

**3. The category claim.** "Portable context layer" as a category is not yet staked by any funded player. Mem0 says "memory." Letta says "agent state." The emerging cloud-memory products say "second brain." Naming the category — and being specific about the T-shaped user — is a positioning moat that only works if claimed early and defended publicly.

**4. Single-binary install.** Every direct competitor ships Docker (OpenMemory, doobidoo), npm (CaviraOSS), cloud accounts (SaaS memory products), or a runtime (Letta Code). A real single-binary install — `brew install tessera`, no Docker, no Postgres, no Qdrant, no account — is a friction asymmetry. Mainstream non-technical users are gated by setup friction; first-mover on zero-friction install matters.

**5. All-local, by absence not by toggle.** fastembed (ONNX Runtime) for both embedding and reranking, fully in-process. No cloud adapters ship; the codebase has no API-key surface to defeat. Most competitors default to OpenAI keys. Aligns structurally with the open-source-LLM movement accelerating in 2026 and removes "cloud is opt-in" from the trust posture in favour of "cloud is absent."

**6. Aesthetic and UX discipline.** Linear vs. Jira. Bear vs. Evernote. Most memory products are dev-tools-ugly. An opinionated, prosumer-quality interface (CLI first, no GUI in v0.1) that competes on shape rather than feature-count is a moat that compounds with brand.

What is *not* a moat: Apache 2.0 license (table stakes), MCP support (everyone has it), local storage (several have it), in-process ONNX inference (commodified by fastembed and similar). Stating these as differentiators would be self-deception.

## Risks — stress-tested

### Risk 1 — A cloud-memory competitor ships a hybrid write-time + query-time architecture

**Scenario.** A funded or distribution-heavy competitor in the cloud-memory class adds a compilation-agent layer over their existing cloud-Postgres backend, becoming SQL + graph + derived wiki layer with broad audience reach.

**Realism.** High. Probably 2–6 months for at least one such product.

**Defense.** The cloud dependency remains. Adding a compilation layer on top of a rented database does not change the architectural fact that the user's data lives on someone else's infrastructure. The sovereignty differentiator holds indefinitely. The audiences also tend to diverge: distribution-led cloud-memory products skew toward content-creator segments; Tessera targets the senior T-shaped engineer who will not adopt a cloud-dependent stack regardless of feature parity. Tessera can pick up the users who internalized the cross-tool framing but won't install a cloud-backed memory layer.

### Risk 2 — Mem0 or Letta pivots into the T-shape positioning

**Scenario.** A funded competitor repositions to target the user segment Tessera is aiming at.

**Realism.** Low for Mem0 (dev-tool-flavored, B2B-shaped). Low for Letta (agent-runtime-shaped). Medium for a new entrant.

**Defense.** SWCR's topology-aware cross-facet coherence weighting is non-trivial to copy; it requires rebuilding retrieval. The single-binary install is hard to achieve with a Python-SDK-shaped product (Mem0's natural shape). The user archetype is specific enough that generic memory positioning won't feel targeted.

### Risk 3 — Provider memory closes the gap

**Scenario.** ChatGPT Memory, Claude Projects, Gemini Personal Intelligence all ship v3-grade cross-conversation memory. Users settle into one provider.

**Realism.** High for partial (already happening). High for full within each provider's ecosystem within 12 months.

**Defense.** Provider memory is explicitly provider-scoped. The more locked-in each provider's memory becomes, the more valuable cross-tool portability gets for the user who is *not* all-in on one provider. This is the Tessera user by definition — T-shaped, multi-tool. Provider memory does not threaten this audience; it validates it.

### Risk 4 — Platform-level identity ships at OS level

**Scenario.** Apple, Google, or Microsoft ships a system-wide AI context layer. Any AI app can read/write shared user context.

**Realism.** Medium for partial in 18–24 months. High for full in 3–5 years.

**Defense.** Cross-platform portability. Apple's version will be Apple-only. Tessera works across macOS, Linux, Windows, and eventually Android. This is years away; monitor, don't over-design for it now.

## Examples — what this looks like in daily use

### Example 1 — The LinkedIn post demo (the v0.1 demo of record)

Tom, AI-native backend engineer, decides to write a LinkedIn post about why his anneal project uses git worktrees for isolation.

**Day 1.** Working in Claude Desktop. Tom teaches Claude: "For LinkedIn, use the 5-act structure — Hook → Legend → Credibility Spike → Observation → Meaning. No emojis. 150–300 words." Claude captures this via `capture(content, facet_type='workflow')`. Tom pastes three recent LinkedIn posts as voice samples. Claude captures each via `capture(content, facet_type='style')`. Tom mentions anneal's architecture (Artifact-Eval-Agent triplet, git worktrees for isolation). Claude captures via `capture(content, facet_type='project')`.

**Day 4.** Tom opens ChatGPT for an unrelated task. Later that week: "Draft me a LinkedIn post about why my anneal project uses git worktrees." ChatGPT, configured with Tessera MCP, calls `recall("LinkedIn post anneal git worktrees")`. Tessera's SWCR retrieval returns a coherent cross-facet bundle — LinkedIn style samples, the 5-act workflow, anneal project context, no-emoji preference — all within the 2K token budget. ChatGPT drafts a post that feels like Tom wrote it, across every dimension, without Tom setting anything up in ChatGPT.

The wow moment is not "ChatGPT remembered one fact Tom told Claude." It's **"ChatGPT produced a draft that feels like mine, synthesized across dimensions, without me configuring ChatGPT."**

### Example 2 — Cross-tool preference propagation

Tom tells Cursor: "I prefer `uv` over `pip` for Python. Never suggest `pip install`." Cursor captures via Tessera. Later, working in Codex CLI on a new project, Tom asks Codex to set up dependencies. Codex queries Tessera, gets the preference, uses `uv` without Tom re-specifying. Same for Pydantic over dataclasses, for async-first I/O, for Go for backend services.

These aren't memory — they're rules of engagement. They should propagate automatically. Tessera makes that the default behavior.

### Example 3 — The T-shape synthesis — vertical substance, horizontal form

Tom is drafting a Reddit comment in r/LocalLLaMA about a paper he's reading on agentic memory. The *substance* is vertical (AI research, his specialty). The *form* is horizontal (Reddit register: terse, slightly abrasive, in-group aware, minor grammatical imperfections OK).

Tom asks any AI tool: "Draft a Reddit comment on this paper: [link]." Tessera's `recall` returns: the AI-research project/identity context (vertical substance), the Reddit comment style samples (horizontal form), the Reddit-specific preferences ("4 sentence max, no transition phrases, no LLM-structural tells"). The resulting draft reflects all three.

No existing memory layer does this. They return facts. Tessera returns a coherent operating model for a specific output type.

## Origin and posture

Tessera is a solo-developer craft project. Built by Tom Mathews and hosted at [`Mathews-Tom/Tessera`](https://github.com/Mathews-Tom/Tessera) (the personal-namespace convention for dev-tool repos). It draws on existing open-source primitives split across two namespaces — [`determ-ai`](https://github.com/determ-ai) for longer-lived project repos (archex, VoxID, docex) and [`Mathews-Tom`](https://github.com/Mathews-Tom) for dev-tool repos (armory, no-magic, mudra, codevigil) — specifically SWCR (retrieval algorithm), archex (graph foundations), mudra (dedup), and the no-magic philosophy throughout.

This is explicitly not a venture-scale opportunity. It is a craft project that Tom will dogfood first, then use in teaching/mentoring contexts, then ship to whoever it turns out to serve after that. Paced by solo-dev evening-and-weekend velocity while a DBA dissertation on agentic memory systems lands in parallel.

Apache 2.0. No CLA. No telemetry. No hosted tier in v0.1. No model reselling. No paid features in the open-source core, ever.

If a real audience forms, the long-term monetization shape would be optional managed sync (BYO storage always free) — the Obsidian Sync playbook. That's years out and not the reason this exists. The reason this exists is that the cross-tool operating-model fragmentation is a real daily tax Tom pays, the engineering shape is interesting, and the existing products all miss the framing.

---

## Reading next

- **System Design** — architecture, schema, retrieval pipeline, MCP surface
- **Release Spec** — what ships in v0.1, v0.3, v0.5, v1.0, and what never ships
- **Pitch** — share-with-colleagues version
