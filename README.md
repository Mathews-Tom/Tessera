# Tessera — Portable context for AI tools

> A local-first context layer for agents and AI tools. Tessera stores durable user and project context in an encrypted SQLite vault, exposes it through a scoped MCP surface, and retrieves cross-facet bundles with hybrid search, rerank, SWCR, and token budgeting.

> Open source. Local-first. Apache 2.0.

---

## Status

`v0.1.0rc1` is live on PyPI as [`tessera-context`](https://pypi.org/project/tessera-context/0.1.0rc1/). The repo contains the packaged Python CLI, encrypted vault, daemon, HTTP MCP endpoint, first-party stdio MCP bridge, connector writers, retrieval pipeline, and test suite. General availability gates on external-user demo validation and cross-platform install recording — both scoped to the v0.1.x → v0.5 stabilization window per the decision on 2026-04-25. The rc1 is install-stable for technical users; expect real-user feedback to drive a follow-up rc before GA. See [`docs/release-spec.md`](docs/release-spec.md) for the release bar and [`docs/v0.1-dod-audit.md`](docs/v0.1-dod-audit.md) for DoD status.

## Install

From PyPI (recommended):

```bash
pip install --pre tessera-context
# or pin explicitly:
pip install tessera-context==0.1.0rc1
```

pip's default resolver skips pre-releases, so `--pre` or an explicit version pin is required. The PyPI distribution name is `tessera-context`; the CLI binary and Python import path remain `tessera`. The short `tessera` name on PyPI is held by a 2017-dormant Graphite dashboard project; PEP 541 reclaim is pursued in parallel.

From source (for development):

```bash
git clone https://github.com/Mathews-Tom/Tessera.git
cd Tessera && uv sync --dev
uv run tessera --help
```

## Core local flow

```bash
# one-time setup: pin a passphrase in your shell so commands run flag-free.
# the vault path defaults to ~/.tessera/vault.db; override with $TESSERA_VAULT.
export TESSERA_PASSPHRASE='your-passphrase-here'   # add to ~/.zshrc or your global .env

tessera init                          # creates ~/.tessera/vault.db
tessera models set --name ollama --model nomic-embed-text --dim 768 --activate
tessera daemon start                  # starts tesserad, picks up the env var
tessera connect claude-desktop        # mints token, writes config
# or wire every detected client in one shot:
tessera connect all
```

The `tessera models set --activate` step registers the embedder Tessera will use; the daemon refuses to start without an active model. `nomic-embed-text` ships with Ollama; pull it once via `ollama pull nomic-embed-text` if you have not already.

`--vault` and `--passphrase` flags are still accepted for one-off use, multi-vault setups, or scripted invocations. Resolution order is `flag → $TESSERA_VAULT / $TESSERA_PASSPHRASE → default`. See [`docs/quickstart.md`](docs/quickstart.md#setup-once) for the env-var setup and [`docs/troubleshooting.md`](docs/troubleshooting.md#multi-vault-disambiguation) for multi-vault disambiguation.

ChatGPT Developer Mode is deferred to v0.1.x because the current ChatGPT flow requires HTTPS/OAuth/canonical HTTP MCP compatibility that Tessera v0.1 does not yet ship. The v0.1 demo flow uses Claude Desktop and Claude Code as the MCP-capable clients.

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
| Walk through install + first capture in ~10 minutes | [`docs/quickstart.md`](docs/quickstart.md) |
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
| Fix an install failure, bad first run, or a broken connector | [`docs/troubleshooting.md`](docs/troubleshooting.md) |

## Posture

This is a solo-developer craft project by Tom Mathews, paced by evening and weekend velocity while a dissertation on agentic memory systems lands in parallel. The v0.1 commitment is explicit; v0.3 and beyond are contingent on real-user signal. There is no telemetry, no hosted service in v0.1, and no model reselling ever. See `docs/non-goals.md` for the full list of things Tessera will not become.

The reason this exists is that the amnesia tax is real for a growing audience — T-shaped users operating across three or more AI tools a week — the engineering shape is interesting, and the adjacent products in the space treat memory as flat blobs in someone else's cloud. Tessera treats it as structured context on disk.

## License

Apache 2.0. No CLA.
