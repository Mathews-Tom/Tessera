# Tessera

## _The soul that outlives the substrate._

---

Every six weeks a new model ships. Opus 4.6 → 4.7. GPT-5.3 → 5.4. Each version shifts capabilities, prompt sensitivities, judgment patterns. If you've built or run any agent that depends on LLM behavior, you've felt it: the agent you tuned last quarter behaves like a stranger this quarter. Same name, different agent.

Now multiply that by the substrate-change problem. You move an agent from Claude to GPT. From cloud to local Qwen. From one harness to another. Whatever you'd taught it — your conventions, your voice, the relationships it had with you and your team — gone. The body changed. The soul didn't survive.

The autonomous-agent community has a phrase for this: _new body, new soul._ Today, that's the default. **Tessera makes it new body, same soul.**

## What Tessera is

Tessera is a substrate-independent identity layer for AI agents. A small daemon runs locally, owning a single SQLite file that holds the agent's identity — segmented into facets: episodic memory, semantic knowledge, voice and writing style, learned skills, working relationships. Any MCP-capable agent connects with a scoped capability token and reads or writes its identity. When the underlying model changes, the new substrate calls `assume_identity()`, gets back a curated bundle of who-this-agent-is, and behaves continuously with the prior one.

The lead user is the agent. The human is one of the agents.

## Why now

Three things are true simultaneously in early 2026:

1. **Model versions ship faster than agents can stabilize.** Six-week release cycles. Material capability shifts within the same provider's ecosystem. The substrate-change problem is getting worse, not better.

2. **Autonomous agents are proliferating.** OpenClaw, Letta Code, Cline, custom harnesses, internal company agents. The population of long-running agents that accumulate state is growing fast. None of them have a clean answer to "how does this agent stay this agent?"

3. **The memory-layer market is saturated for _fact recall_ — but nobody is solving identity continuity.** Mem0 has $24M and a memory passport. Letta has a stateful agent runtime. MemPalace has 96.6% on LongMemEval. Cognee has 30+ data connectors. Every one of them treats memory as a flat fact store. Not one of them treats it as the agent's continuity of self across substrate changes. That framing is open.

## Why this and not Mem0 / OpenMemory / Letta

Honest read: those products solve different problems.

- **Mem0 / OpenMemory** is a memory layer that other AIs call into. Tessera competes here on framing (identity, not memory) and on retrieval depth (SWCR-based topology-aware retrieval, vs. their hybrid-vector-graph). Also single-binary install vs. their Docker + Postgres + Qdrant stack.
- **Letta / Letta Code** is an agent runtime that _replaces_ Claude Code. Tessera doesn't replace anything — it's the identity layer that any harness, including Letta, can consume.
- **Provider memory** (ChatGPT, Gemini Personal Intelligence, Claude Projects) is single-provider lock-in. Tessera is the inverse — explicitly built to make leaving any provider painless.

The comparison isn't "Tessera vs. Mem0." It's "Tessera and Mem0 solve adjacent problems with different framings." If the agent-identity framing catches, it becomes a category. The category is open.

## Technical depth where it matters

Three architectural commitments make this real, not slideware:

1. **Per-model embedding tables.** Switching embedders creates a new vec table; old embeddings stay queryable until pruned. Model-portability at the storage layer, not just the API.

2. **SWCR-based retrieval.** Topology-aware multi-agent retrieval — unpublished dissertation work — applied directly to identity reconstruction. The `assume_identity` call doesn't dump the top-K facets; it returns a _coherent_ bundle (voice that matches recent context, skills that match active goals, episodics that ground current relationships). This is the differentiator that doesn't lift-and-shift.

3. **All-local default.** Ollama for embedding, extraction, and reranking. Cloud is opt-in. The full stack runs offline, on a plane, when OpenAI is down. Aligned with the open-source-LLM movement that's accelerating in 2026.

## Posture

This is a solo-dev craft project. Built on existing open-source primitives — SWCR (retrieval), archex (graph foundations), mudra (dedup) — drawn from the same ecosystem. Apache 2.0. No CLA. No telemetry. No funding aspirations. No hosted tier in v0.1. No paid features in the open-source core, ever.

If a real audience forms, the long-term monetization shape would be optional managed sync (BYO storage is always free) — the Obsidian Sync playbook. That's a years-out concern. The reason this exists is that the substrate-change problem is real, the engineering shape is interesting, and the existing products in the space all miss the framing.

## What I'm asking from you

I want this to fail fast if it's going to fail. Three specific reactions would be useful, in order:

1. **Does the framing land?** Does "agent identity that survives the substrate" feel like a real category, or does it feel like a memory product with a paint job? Be blunt.

2. **Where does the demo break in your head?** When you imagine the model-swap demo path (agent on Sonnet 4.6 → swap to 4.7, same voice, same context, same skills), what's the part you don't believe?

3. **Who would actually use this in your network?** Not "who would think it's interesting." Who would change their setup to install it. Anyone running long-running agents, custom harnesses, or autonomous workflows. If the answer is "nobody I know," that's a real signal.

I'd rather hear "this is solving a problem nobody has" now than build for six months and find out at launch.

---

**Tessera** — _The soul that outlives the substrate._
_Persistent agent identity that survives every model change._

Apache 2.0. Local-first. Single binary. No telemetry.
