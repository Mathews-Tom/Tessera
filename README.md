# Tessera — The soul that outlives the substrate.

> Persistent agent identity that survives every model change. Memory, voice, and skills that travel with your agent — across Claude, GPT, Gemini, local Qwen, or whatever ships next month.

> Open source. Local-first. Apache 2.0.

---

## Status

Tessera is in pre-alpha design. Code has not been written yet; the current focus is finishing the foundational specifications against which v0.1 will be built. See [`docs/release-spec.md`](docs/release-spec.md) for the shipping plan.

## What is Tessera

A substrate-independent identity layer for AI agents. A local daemon owns a single-file SQLite vault that holds the agent's identity — episodic memory, semantic knowledge, voice and writing style, learned skills, working relationships. Any MCP-capable agent connects with a scoped capability token and reads or writes its identity. When the underlying model changes, the new substrate calls `assume_identity()`, gets back a curated bundle of who-this-agent-is, and behaves continuously with the prior one.

The lead user is the agent. The human is one of the agents.

## Where to read, by role

| If you want to | Read |
|---|---|
| Pitch to a colleague or evaluate whether this is interesting | [`docs/pitch.md`](docs/pitch.md) |
| Understand the market position, category claim, and trade-offs | [`docs/system-overview.md`](docs/system-overview.md) |
| Understand the architecture, schema, retrieval pipeline, encryption | [`docs/system-design.md`](docs/system-design.md) |
| Understand the SWCR retrieval algorithm and its ablation bar | [`docs/swcr-spec.md`](docs/swcr-spec.md) |
| Understand the security model and threat analysis | [`docs/threat-model.md`](docs/threat-model.md) |
| Understand how migrations are safe | [`docs/migration-contract.md`](docs/migration-contract.md) |
| Understand how debuggability works without telemetry | [`docs/determinism-and-observability.md`](docs/determinism-and-observability.md) |
| Know what ships in v0.1, v0.3, v0.5, v1.0 | [`docs/release-spec.md`](docs/release-spec.md) |
| Know what will never ship | [`docs/non-goals.md`](docs/non-goals.md) |
| Review the load-bearing decisions | [`docs/adr/`](docs/adr/) |

## Posture

This is a solo-developer craft project by Tom Mathews, paced by evening and weekend velocity while a dissertation on agentic memory systems lands in parallel. The v0.1 commitment is explicit; v0.3 and beyond are contingent on real-user signal. There is no telemetry, no hosted service in v0.1, and no model reselling ever. See `docs/non-goals.md` for the full list of things Tessera will not become.

The reason this exists is that the substrate-change problem is real for a narrow but growing audience (long-running autonomous agents, developers running custom harnesses), the engineering shape is interesting, and the existing products in the space all miss the framing.

## License

Apache 2.0. No CLA.
