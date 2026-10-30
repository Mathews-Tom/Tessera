# AGENTS.md

## Code Review Rules

- **What failure means here:** `src/tessera/auth/tokens.py` accepts expired/revoked capabilities or breaks refresh/rotation, leaving managed MCP clients unable to authenticate.
- Vault encryption/key handling in `src/tessera/vault/{encryption,connection}.py` exposes secrets, accepts the wrong key, or leaves persisted context unreadable.
- Schema changes in `src/tessera/vault/schema.py` and `src/tessera/migration/runner.py` strand existing vaults, lose data, or break backup/resume safety.
- `src/tessera/mcp_surface/tools.py` or `src/tessera/vault/compiled.py` returns context outside the caller's scopes or agent ownership, including lookups by a leaked external ID.
- **Do not report:** style, formatting, naming nits, or diagnostics already enforced by Ruff lint/format or strict mypy; speculative hardening without a concrete failing scenario; unchanged-code issues as required fixes (label any necessary context Pre-existing); findings already raised on an earlier commit of this PR.
- **Paths to skip:** `**/__pycache__/` and recorded benchmark outputs `docs/benchmarks/**/results/*.json`; review the source/configuration that produces them instead. `uv.lock` stays in scope for dependency changes: flag packages added or version-changed without a matching `pyproject.toml` change; do not review it line by line.
- **Evidence and format:** every finding must cite `file:line`, name the bug class, and describe the concrete failing scenario and impact supported by the code, not a guess from a name. Group instances of the same bug class so the author can fix all occurrences.
- Tag every finding Important / Nit / Pre-existing; reserve Important for actionable failures introduced by this PR, and do not manufacture Nit findings excluded above. Start the summary with the count of Important findings; if zero, say plainly that there is nothing to fix.
