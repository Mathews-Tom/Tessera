# Changelog

All notable changes to Tessera are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] — v0.1.0-pre

### Client connectors

- Five MCP client connectors: Claude Desktop, Claude Code, Cursor,
  Codex (TOML), and ChatGPT Developer Mode. `tessera connect <client>`
  mints a capability token, resolves the client's platform-specific
  default config path, and writes the Tessera MCP-server entry with a
  pre-write backup and atomic replace.
- `tessera disconnect <client>` removes the Tessera entry without
  stomping sibling keys the user authored. Missing-file and
  already-absent paths are no-ops.
- ChatGPT Dev Mode ships with an in-daemon one-time-use nonce store
  and `POST /mcp/exchange` endpoint. The CLI mints a session token,
  asks the daemon to stash it under a 192-bit nonce with a 30-second
  TTL, and prints the bootstrap URL the user pastes into ChatGPT. The
  raw token never appears in the URL per ADR 0007.
- New CLI flags: `tessera connect <client> --vault <path> --agent-id N`
  with optional `--url`, `--token-class`, `--path`, `--socket`,
  `--passphrase`. `tessera disconnect <client>` takes `--path` to
  override the default.

### Reframe reconciliation

The April 2026 product reframe shifted Tessera from an agent-identity
substrate-swap layer to a portable context layer for the T-shaped
AI-native user. This release brings the P1–P9 codebase in line with
the post-reframe decision layer ([ADR 0010](docs/adr/0010-five-facet-user-context-model.md),
[ADR 0011](docs/adr/0011-swcr-default-on-cross-facet-coherence.md)).

### Added

- Schema v2 with the five-facet v0.1 vocabulary (`identity`,
  `preference`, `workflow`, `project`, `style`) plus reserved v0.3
  (`person`, `skill`) and v0.5 (`compiled_notebook`) types per ADR
  0010.
- Forward-migration script v1 → v2 that remaps retired facet types
  (`episodic` → `project`, `semantic` → `preference`,
  `relationship` → `person`, `goal` → `project`), drops `judgment`
  rows, introduces the `mode` column, renames `source_client` to
  `source_tool`, and creates the reserved `compiled_artifacts`
  table.
- `forget` MCP tool — soft-delete with an audit entry; replaces the
  retired `assume_identity` slot in the six-tool surface.
- `tessera forget <external_id> [--reason]` CLI passthrough.
- CI grep gates `facet_vocabulary_grep.sh` and `assume_identity_grep.sh`
  blocking reintroduction of retired vocabulary.
- B-RET-3 harness measuring cross-facet `recall(facet_types=all)`
  bundle-assembly latency (replaces the retired `assume_identity`
  latency benchmark).

### Changed

- `retrieval_mode` default flipped from `rerank_only` to `swcr`
  per ADR 0011. The ablation arms remain fully wired.
- `recall` without an explicit `facet_types` filter now fans out
  across every facet type the caller is scoped to read.
- `tessera capture --facet-type` is now required; there is no sensible
  default under the five-facet model.
- B-RET-1 dataset generator emits the five v0.1 facet types at a
  realistic mix (identity 5%, preference 15%, workflow 15%, project
  30%, style 35%).

### Removed

- `assume_identity` MCP tool, the `src/tessera/identity/` module, the
  identity-bundle role map, and all associated audit ops.
- Pre-reframe facet types (`episodic`, `semantic`, `relationship`,
  `goal`, `judgment`) from schema CHECK, scope allowlists, and write
  paths.

### Fixed

- Dispatcher no longer defaults to `("style", "episodic")` on recall;
  it resolves to the caller's scoped-for-read set.

## Released versions

No public releases yet. `v0.1.0` ships when the definition-of-done in
`docs/release-spec.md §v0.1 DoD` is fully green.
