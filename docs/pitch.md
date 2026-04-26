# Tessera

## *Teach it once. Use it anywhere.*

---

You've explained your preferences to Claude. You've written a CLAUDE.md. You've trained ChatGPT's custom instructions. You've configured Cursor's rules. You've told Gemini how you write. You've done this twelve times this year and you'll do it again in six weeks when something new ships.

Every AI tool you use is an amnesiac you re-onboard from scratch. Your preferences, your workflows, the projects you're in the middle of, the voice you write in — they live in your head and in a different config file for every tool you touch. There is no layer between you and the models. The moment you switch tools, you start over.

Tessera is that layer.

## What Tessera is

A local daemon owns a single file on your disk. That file contains your *context*: who you are, how you prefer to work, the workflows you follow, the projects you're in, the voice you write in. Any AI tool that speaks MCP can read and write that context with a scoped capability token. You teach it once. Every tool uses it.

When Claude learns you prefer `uv` over `pip`, Cursor inherits that. When ChatGPT drafts a LinkedIn post, it uses the 5-act structure you taught Claude. When a new model ships next month and you swap, nothing is lost — your context isn't locked to a model. It's yours, on your disk, portable.

The name is deliberate: a *tessera* was the Roman token of identity you carried to prove who you were. One tile is small; the assembled mosaic is your full picture. That's the shape of the thing.

## Who it's for

The archetypal Tessera user is the **T-shaped AI-native engineer**: deep vertical expertise in one or two domains, active horizontal engagement across many others through AI tools.

Concretely: I'm an AI-native backend engineer. I go deep on backend systems and on AI research. But my day also involves front-end design decisions, database choices, blog posts, Reddit comments, LinkedIn posts, mentoring conversations. Every one of those is AI-assisted. Every one of them currently starts with me re-explaining how I work.

If you're reading this and thinking "that's me but with different domains" — you're the user. Built for people deep in a domain who also operate across many, all AI-assisted, who've felt the amnesia tax.

## Why this is different from what exists

Two adjacent product classes, two honest comparisons:

| Class | What it does | Why Tessera is different |
|---|---|---|
| **Per-tool preference files** (CLAUDE.md, ChatGPT custom instructions, Cursor rules, Codex config) | One preference file per tool | One tool only. Forget Claude's custom instructions exist when you're in ChatGPT. Tessera is cross-tool by design. |
| **Cloud-hosted memory layers** (Mem0, OpenMemory, MemPalace, Cognee, and the emerging cloud-Postgres "second brain" products) | Memory layer (fact recall), typically served from a cloud database | Two differences. **Shape:** they treat memory as a flat blob; Tessera treats it as *structured context* — preferences, workflows, projects, style — so retrieval returns coherent bundles, not nearest-neighbor facts. **Sovereignty:** your context lives in their database; with Tessera it lives in a single file on your disk. Every other difference is downstream of those two. |

## The write-time / query-time frame

Every AI knowledge system has to answer one question: when does the AI do the hard thinking? Write-time (compile on ingest, like Karpathy's personal-wiki prompt), or query-time (store cheaply, synthesize when asked, like most memory products today)?

Tessera is **query-time by default, for the horizontal touch of the T-shape.** Preferences, workflows, project context, style — these are rules of engagement, not narrative synthesis. They want to be stored cleanly and retrieved coherently at query time. Write-time compilation is the wrong tool for "do I use `uv` or `pip`."

For the **vertical depth of the T-shape** — your long-running research, your deep work, your evolving thinking — write-time compilation is the right tool. v0.5 ships it as a new facet type (`compiled_notebook`): you tag a project or skill as vertical-depth, and a compilation agent writes a synthesized artifact from those source facets. The v0.1 schema reserves the facet type and the `compiled_artifacts` table, so the transition is additive, not a rewrite. v0.1 is honest about scope: horizontal touch only. If your deep vertical needs wiki-style compilation today, Karpathy's prompt does it well; nothing in Tessera conflicts with running that alongside.

## What makes it technically real

Three architectural commitments that aren't marketing copy:

1. **Single file, not a service.** The vault is a SQLite database at `~/.tessera/vault.db`. You can `cp` it. Email it. Inspect it with any SQLite browser. No Docker. No Postgres. No Qdrant. No account. The file is the product.

2. **SWCR-based cross-facet retrieval.** When you ask ChatGPT to "draft a LinkedIn post about my anneal project," you don't want nearest-neighbor facts — you want your LinkedIn writing voice, your project context, your 5-act workflow, your preferences, *together*, coherent. That's what SWCR (Sequential Weighted Context Recall) delivers. It's topology-aware cross-facet coherence weighting done at query time, not a cosine search dressed up as retrieval.

3. **All-local by default.** fastembed (ONNX Runtime) for both embedding and reranking, fully in-process inside the daemon. No model server, no cloud, no torch. The stack runs on a plane. The DX-pain movement away from hosted model providers is accelerating; Tessera aligns with it structurally, not as a toggle.

## Posture

Solo-dev craft project. Built by Tom Mathews, on evenings and weekends, while a dissertation on agentic memory systems lands in parallel. Drawing on existing open-source primitives: SWCR (the retrieval algorithm), archex (graph foundations), mudra (dedup). Apache 2.0. No CLA. No telemetry. No hosted tier in v0.1. No paid features in the open-source core, ever.

Not venture-scale. Not trying to be. Built to be used — by me first, by people like me next, by whoever it turns out to serve after that. If a real audience forms, the long-term shape is optional managed sync (BYO storage always free), which is the Obsidian Sync playbook. That's a years-out concern.

## What I'm asking from you

I want this to fail fast if it's going to fail. Three specific reactions would be useful:

1. **Does the framing land?** Does "portable context layer for every AI tool" feel like a category, or does it feel like a memory product with a paint job? Be blunt.

2. **Where does the demo break in your head?** When you imagine capturing a LinkedIn workflow in Claude and having ChatGPT produce a post in your voice using the right structure — what's the part you don't believe?

3. **Who would actually use this in your network?** Not "who would think it's interesting." Who would change their setup to install it. T-shaped engineers, people running 3+ AI tools, people who've written a CLAUDE.md and wished it worked everywhere. If the answer is "nobody I know," that's a real signal and I'd rather hear it now than at launch.

---

**Tessera** — *Teach it once. Use it anywhere.*
*Portable context that travels with you across every AI tool.*

Apache 2.0. Local-first. Single binary. No telemetry.
