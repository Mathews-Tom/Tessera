# Troubleshooting

Symptom-indexed guide for Tessera v0.1.0rc1. If your symptom isn't here and `tessera doctor` doesn't localise the problem, collect a diagnostic bundle (`tessera doctor --collect`) and open a GitHub issue at https://github.com/Mathews-Tom/Tessera/issues.

---

## Install failures

### `ERROR: Could not find a version that satisfies the requirement tessera-context`

`pip` skips pre-release versions by default. On rc1 you must either pass `--pre` or pin the exact version:

```bash
pip install --pre tessera-context
# or
pip install tessera-context==0.1.0rc1
```

This goes away once `0.1.0` GA ships.

### `ERROR: No matching distribution found for tessera`

The PyPI *distribution* name is `tessera-context`, not `tessera`. The CLI binary and Python import path are both `tessera`, but `pip install` needs the distribution name. The short `tessera` name is held by a 2017-dormant Graphite project; PEP 541 reclaim is pursued separately.

### `ERROR: Python 3.12+ required` / `could not find a version compatible with this Python`

`pyproject.toml` pins `requires-python = ">=3.12,<3.13"`. Check:

```bash
python3 --version
```

If < 3.12, install 3.12. On macOS: `brew install python@3.12`. On Ubuntu 22.04 (ships 3.10): use [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) or `pyenv`. On Windows: [python.org installer](https://www.python.org/downloads/).

The upper-bound pin (`<3.13`) is because some transitive deps (notably `sqlcipher3`) don't ship 3.13 wheels yet. When those deps catch up, the cap lifts.

### `sqlcipher3 ... fatal error: 'sqlcipher/sqlite3.h' file not found` (macOS)

On macOS, `sqlcipher3` compiles against Homebrew's `libsqlcipher`. Install it and re-run the pip install:

```bash
brew install sqlcipher
pip install --pre --force-reinstall tessera-context
```

On Apple Silicon the compiler needs to find Homebrew's `/opt/homebrew` prefix, not the Intel `/usr/local`. Export paths for the pip install:

```bash
export CFLAGS="-I$(brew --prefix sqlcipher)/include"
export LDFLAGS="-L$(brew --prefix sqlcipher)/lib"
export LIBSQLCIPHER_PATH="$(brew --prefix sqlcipher)"
pip install --pre tessera-context
```

If installing via Homebrew (`brew install --build-from-source packaging/homebrew/Formula/tessera.rb`), the formula sets these automatically.

### Install succeeds but `tessera: command not found`

pip installed the distribution but the `tessera` console script isn't on `$PATH`. Common causes:

- `pip install --user` — console scripts land in `~/.local/bin/` (Linux/macOS) or `%APPDATA%\Python\Python312\Scripts\` (Windows). Add it to `$PATH`.
- `pipx install tessera-context --pip-args="--pre"` — makes this a one-step install with isolated venv + PATH wiring.
- Homebrew install — the formula symlinks to `$(brew --prefix)/bin/tessera`; make sure that directory is on `$PATH`.

Verify:

```bash
python3 -c "import tessera.cli.__main__; print(tessera.cli.__main__.__file__)"
which tessera
```

---

## First-run failures (`tessera init` / `tessera daemon start`)

### `passphrase required; pass --passphrase or export TESSERA_PASSPHRASE`

Every command that opens the vault needs a passphrase. The CLI resolves it in this order:

1. `--passphrase <value>` flag.
2. `$TESSERA_PASSPHRASE` (or whatever name `$TESSERA_PASSPHRASE_ENV` points at).
3. Error.

For solo-developer setups, exporting the env var once in `~/.zshrc`, `~/.bashrc`, or your shell-sourced global `.env` is the intended path — every subsequent command runs flag-free:

```bash
export TESSERA_PASSPHRASE='your-passphrase-here'
tessera init                # picks up the env var
tessera daemon start        # same
```

For one-off invocations (CI, scripts, multi-tenant machines), pass `--passphrase` per call instead. See `quickstart.md §Setup once`.

### Multi-vault disambiguation

If `~/.tessera/` contains more than one `*.db` file and you have not set `--vault` or `$TESSERA_VAULT`, the CLI refuses to guess and fails with:

```text
✗ ERROR ~/.tessera contains multiple vaults (~/.tessera/work.db, ~/.tessera/personal.db); pass --vault or export TESSERA_VAULT to pick one
```

Pick the vault you want either per-command (`--vault ~/.tessera/work.db`) or persistently:

```bash
export TESSERA_VAULT="$HOME/.tessera/work.db"
```

The single-vault default (`~/.tessera/vault.db`) covers the v0.1 lead-user case; the disambiguation only triggers when you have explicitly created additional vaults.

### `tessera init` prompts for a passphrase — what should I set?

The passphrase derives the sqlcipher key via argon2id. It's what encrypts your vault at rest. It's stored in your OS keyring (macOS Keychain, GNOME keyring, Windows Credential Manager) after the first run; subsequent commands retrieve it without prompting.

Rules:

- No minimum length enforced by the CLI, but the threat model assumes ≥ 16 chars.
- Losing the passphrase means losing the vault — there's no recovery path. Back it up in your password manager.
- You can change it later via `tessera vault rekey`.

### `tessera doctor` flags `keyring: unavailable`

Some headless Linux sessions lack a running keyring daemon. Three options:

1. **Start a keyring daemon.** On Ubuntu / Debian: `sudo apt install gnome-keyring && dbus-update-activation-environment --all`. On Fedora: `sudo dnf install gnome-keyring`.
2. **Use an alternate backend.** `pip install keyrings.alt` provides `PlaintextKeyring` (file-backed, 0600, not encrypted — OK on a single-user machine you control, not OK on shared hosts).
3. **Use `$TESSERA_PASSPHRASE` directly** — Tessera reads this env var on every CLI invocation when no `--passphrase` flag is given, so headless hosts without a working keyring can still operate flag-free.

### `tessera daemon start` fails with `NoActiveModelError: no embedding model is flagged active`

`tessera init` bootstraps the vault but does not register an embedder. The daemon refuses to start without an active model because the embed worker and retrieval pipeline have nothing to call. Register one and flag it active:

```bash
tessera models set --name ollama --model nomic-embed-text --dim 768 --activate
tessera daemon start
```

`--name` is the adapter id (only `ollama` ships in v0.1), `--model` is the Ollama model name, `--dim` matches the model's embedding dimensionality (768 for `nomic-embed-text`), and `--activate` flips the row's `is_active` flag. If you registered a model previously but did not activate it, run the same command again with `--activate` — the registry upserts on `(name, model)` and only one row can be active at a time.

If `nomic-embed-text` is not present locally, `ollama pull nomic-embed-text` first; the prerequisites in `quickstart.md` cover this.

### `Address already in use` / `tessera daemon start` crashes with `OSError: [Errno 48]`

Something is already bound to port `5710` (the default HTTP MCP port). Check:

```bash
lsof -i :5710       # macOS / Linux
netstat -ano | findstr :5710   # Windows
```

If it's a prior `tesserad` that didn't shut down cleanly: `tessera daemon stop` (idempotent since PR #23). If it's another process entirely, either stop that process or pick a different port:

```bash
tessera daemon start --port 5711
# or
export TESSERA_HTTP_PORT=5711
tessera daemon start
```

All subsequent `tessera connect <client>` invocations must use the same port (`--port 5711`).

### `tessera doctor` flags `ollama: unreachable`

Tessera's default embedder is Ollama. If `ollama serve` isn't running or isn't reachable on `http://localhost:11434`:

1. Start it: `ollama serve` (foreground) or `brew services start ollama` (macOS background).
2. Pull the default embedding model: `ollama pull nomic-embed-text`.
3. Re-run `tessera doctor`; the check should flip green.

If you run Ollama on a non-default host or port, point Tessera at it:

```bash
export TESSERA_OLLAMA_HOST=http://192.168.1.50:11434
tessera daemon start
```

### `tessera doctor` flags `sqlite-vec: not loaded`

The `sqlite-vec` extension registers the `vec0` virtual-table type used for dense retrieval. It's a runtime dep pinned in `pyproject.toml` and should load automatically. If the doctor check fails, most likely:

- Running an ancient Python 3.12 build that's missing `enable_load_extension`. Upgrade: `brew upgrade python@3.12` or `pyenv install 3.12.8`.
- On very locked-down distros, extension loading is disabled at the sqlite3 compile level. Rebuild Python or switch to a pyenv-managed 3.12.

### `tessera doctor` flags `facet_types: empty vocabulary`

`tessera init` ships the v0.1 facet-type vocabulary (`identity | preference | workflow | project | style`) into the vault. An empty-vocabulary error means either:

- `tessera init` wasn't run against this vault. Run it.
- The vault was created manually via SQL and skipped the migration. Open it with `tessera vault migrate <vault.db>` to apply the pending schema rows.

---

## Connector / client issues

### Claude Desktop: "Server disconnected" right after startup

Claude Desktop uses the stdio MCP bridge (`tessera stdio --url ... --token ...`). If it immediately disconnects, the bridge is crashing before the first `tools/list` response. Enable the debug env var in the config:

Edit your `claude_desktop_config.json` (path below) and find the `tessera` entry. Add to `env`:

```json
"env": {
  "TESSERA_STDIO_BRIDGE_DEBUG": "1"
}
```

Restart Claude Desktop. The bridge will now print a traceback to stderr on failure. Claude Desktop's log file captures it:

- macOS: `~/Library/Logs/Claude/`
- Linux: `~/.local/state/Claude/logs/`
- Windows: `%LOCALAPPDATA%\Claude\logs\`

Common causes:

- **Daemon isn't running.** `tessera daemon status`. If not running, `tessera daemon start` in a separate terminal.
- **Token expired.** Session tokens default to a 30-minute idle TTL; Claude Desktop stashes the token at connect time. Re-run `tessera connect claude-desktop` to issue a fresh token.
- **URL mismatch.** The `--url` in the config must match the daemon's actual bind (`DEFAULT_HTTP_PORT = 5710` unless overridden).

### Claude Code: my `~/.claude/claude_code_config.json` still has the old Tessera entry

That's the wrong path. `claude-code` reads MCP servers from `~/.claude.json` (top-level, not under a `claude` subdirectory). Pre-PR #23 we wrote to the wrong file; rc1 fixes it.

Clean up:

```bash
# remove the stale file (backup first if you have other entries)
rm ~/.claude/claude_code_config.json

# re-wire via the current connector
tessera connect claude-code
```

### Claude Desktop on Linux/Windows: `claude_desktop_config.json` is missing

Claude Desktop writes the config on first launch. If you ran `tessera connect claude-desktop` before ever launching Claude Desktop, the parent directory may not exist yet. Either launch Claude Desktop once (it creates the parent directory) or rerun `tessera connect claude-desktop` — the connector creates parent directories as needed in rc1.

Config locations for reference:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

### ChatGPT Developer Mode: connector says "deferred to v0.1.x"

Correct — ChatGPT Developer Mode requires HTTPS/OAuth/canonical HTTP MCP compatibility that Tessera v0.1 doesn't ship. The v0.1 demo flow uses Claude Desktop or Claude Code as the MCP client. ChatGPT Developer Mode support re-opens at v0.1.x once its transport requirements settle.

### Codex: entry written to `~/.codex/config.toml` but not picked up

Codex reads MCP servers from the `[mcp.servers.tessera]` table. Check that Tessera wrote to the right table and that Codex is looking for that table (not `[mcp_servers.tessera]`, which is a different variant some Codex builds use). If Codex's build expects the underscore variant, file an issue with the Codex version you're running.

---

## Capture / recall issues

### `tessera tools call capture ...` succeeds but `recall` returns no matches

Two typical causes, in order of likelihood:

1. **Embedding backlog.** Capture writes facet rows synchronously but embeds asynchronously via the embed worker. On a cold daemon, the first capture may sit unembedded for a second or two. `tessera doctor` reports `embed_backlog` — if it's non-zero, wait a moment and retry. The worker processes at roughly 20–25 facets/sec on reference hardware.
2. **Scope mismatch.** The token you're using for `recall` might not have `read` scope on the facet type you captured with. `tessera tokens list` shows scopes; if you captured with a token scoped to `write=['style']` but recall with a token scoped to `read=['project']`, no matches. Issue a token with `read=['identity','preference','workflow','project','style']` for the full cross-facet bundle.

### `recall` is much slower than the documented tier

Check, in order:

- `tessera doctor` → `embed_backlog` high? Embed worker is saturated; latency will catch up once the backlog drains.
- `tessera doctor` → `retrieval_rerank_degraded` audit events? The sentence-transformers reranker is failing; the pipeline falls back to RRF-only ordering, which doesn't match the benchmarked latency tier.
- Vault size vastly larger than the tier's cohort? 10K facets is the "steady-state" tier; 100K+ would be outside v0.1's measured range.

Cite the vault size + `tessera doctor` output when opening an issue.

### `capture` is silently ignored (no row in vault, no error)

That's a bug — capture is supposed to fail loudly on any write that can't land. If you see this, collect a diagnostic bundle (`tessera doctor --collect`) and open an issue immediately. The audit log at `~/.tessera/events.db` has the request trace if capture reached the daemon at all.

---

## Token / auth issues

### `401 Unauthorized` from every MCP call

Your bearer token isn't valid. Possibilities:

- **Expired** (session tokens: 30 min idle, 8 hr absolute). Mint a new one:

  ```bash
  tessera tokens create --read-scope=identity,preference,workflow,project,style --write-scope=identity,preference,workflow,project,style
  ```

- **Revoked.** `tessera tokens list` shows revocation state. If revoked, issue a new one.
- **Wrong token copied in.** Token strings are long (`tessera_session_<48-char-ulid>`). A trailing whitespace or missing character kills verification.

### `403 Forbidden: scope_denied` on a specific MCP tool

Your token is valid but lacks the required scope for the tool or facet type. Examples:

- `capture(facet_type="identity")` with a write scope of only `['style']` → denied.
- `recall(facet_types=['preference'])` with read scope of only `['identity']` → denied.

Re-issue the token with a scope that covers the operation, or pass a scope-narrower method if you can.

### `403` on `/mcp` specifically (not a tool-call scope denial)

The `Origin` header on your HTTP request isn't in the daemon's allowlist. Native MCP clients don't send an `Origin` header and are fine; browser-driven clients do. If you need a browser origin:

```bash
export TESSERA_ALLOWED_ORIGINS=http://localhost,null,http://127.0.0.1
tessera daemon start
```

Never add `"*"` — the allowlist is the CSRF gate and a wildcard defeats it.

---

## Daemon lifecycle oddities

### `tessera daemon status` says `not-running` but I see a stale socket file

The daemon wrote the socket but crashed before cleanup. Resolve:

```bash
tessera daemon stop    # idempotent; clears pid + socket if process is gone
tessera daemon start
```

The stop command (PR #23) handles the stale-state case without erroring.

### `tessera daemon stop` hangs

The daemon is either busy (large embed backlog draining) or stuck on the control socket. If it hangs > 30 seconds, send SIGTERM:

```bash
pid=$(cat ~/.tessera/run/tesserad.pid)
kill -TERM "$pid"
tessera daemon stop   # cleanup pid + socket files
```

If this recurs, collect a diagnostic bundle and open an issue.

### The daemon keeps dying after startup

Check `~/.tessera/log/tesserad.log` — the last 100 lines usually have the cause. Common culprits:

- Ollama went away mid-embed-pass.
- Vault schema mismatch after a Tessera upgrade. `tessera vault migrate <vault.db>` applies pending migrations.
- Disk full. Vault is a single file; sqlite refuses writes when the filesystem is full.

---

## Observability — what to attach to a bug report

```bash
tessera --version
tessera doctor                     # the text output
tessera doctor --collect ~/bundle  # zipped bundle with content-scrubbed event samples
```

The `--collect` bundle contains:

- Recent `recall_slow` events (sampled)
- Recent `embed_backlog` events (sampled)
- Recent `retrieval_rerank_degraded` events (sampled)
- `daemon_warmed` events (startup history)
- Redacted vault stats (facet counts per type, no content)

No facet content leaves your machine — the bundle is scrubbed per `docs/determinism-and-observability.md §Diagnostic bundle content scrubbing`. Inspect the bundle before attaching if you want to confirm.

Logs live at:

- Daemon log: `~/.tessera/log/tesserad.log`
- Events database: `~/.tessera/events.db` (SQLite; inspect with any SQLite browser)
- Audit log: inside the vault (`SELECT * FROM audit_log` after opening with `tessera vault repl`)

---

## Still stuck?

Open an issue with:

1. `tessera --version`
2. Full output of `tessera doctor`
3. A redacted `tessera doctor --collect` bundle (optional but dramatically speeds up triage)
4. The exact command you ran and the exact error you saw
5. Your OS, Python version, and install path (pip, Homebrew, source)

https://github.com/Mathews-Tom/Tessera/issues
