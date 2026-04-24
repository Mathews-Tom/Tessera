# Tessera — Portable context for AI tools

> A local-first context layer for agents and AI tools. Tessera stores durable user and project context in an encrypted SQLite vault, exposes it through a scoped MCP surface, and retrieves cross-facet bundles with hybrid search, rerank, SWCR, and token budgeting.

> Open source. Local-first. Apache 2.0.

---

## Status

Tessera is a **developer preview**, not a general release. The repo contains the packaged Python CLI, encrypted vault, daemon, HTTP MCP endpoint, stdio bridge, connector writers, retrieval pipeline, and test suite. v0.1 remains gated on clean-VM install and an external-user demo. See [`docs/release-spec.md`](docs/release-spec.md) for the release bar.

Install from source during the preview:

```bash
uv sync --dev
uv run tessera --help
```

Core local flow:

```bash
uv run tessera init --vault ~/.tessera/vault.db
uv run tessera daemon start --vault ~/.tessera/vault.db
uv run tessera connect claude-code --vault ~/.tessera/vault.db
```

ChatGPT Developer Mode is deferred to v0.1.x because the current ChatGPT flow requires HTTPS/OAuth/canonical HTTP MCP compatibility that Tessera v0.1 does not yet ship.

## What is Tessera

A local daemon owns a single-file SQLite vault that holds five v0.1 context facets:

- `identity` — stable user facts
- `preference` — behavioral rules and tool preferences
- `workflow` — repeated procedures
- `project` — active work context
- `style` — writing voice samples

MCP-capable tools connect with scoped capability tokens and call six tools: `capture`, `recall`, `show`, `list_facets`, `stats`, and `forget`. A bare `recall` searches every facet type the token can read, then returns a budgeted cross-facet bundle.

The lead user is the AI-native developer who wants durable context across Claude Code, Claude Desktop, Cursor, Codex, local model workflows, and custom harnesses without handing memory to a hosted service.

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
