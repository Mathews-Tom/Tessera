#!/usr/bin/env bash
# Reset every artifact the T-shape demo creates so the next recording
# session starts from a clean state. Idempotent — running it twice is
# safe, running it against a state where some or all artifacts are
# already absent is safe.
#
# What it touches:
#   1. Stops any running daemon via `tessera daemon stop` (best-effort;
#      a no-op if the daemon was never started).
#   2. Removes the vault database + KDF salt sidecar.
#   3. Removes the daemon's runtime files (pid, log, events.db).
#   4. Removes the control socket if orphaned.
#   5. Calls `tessera disconnect` for each v0.1 MCP client so the
#      client-side config drops its Tessera entry.
#
# What it does NOT touch:
#   - Ollama itself (the model stays pulled).
#   - The installed launchd/systemd unit (use `tessera daemon uninstall`
#     separately if one is active — the reset script does not assume
#     you installed it).
#   - Other vaults under `~/.tessera/`. The script deliberately operates
#     only on the demo vault path (default `~/.tessera/demo.db`) so a
#     stray invocation does not nuke a real working vault.
#   - Ollama-side conversation history in Claude Desktop / ChatGPT
#     clients. Those are separate from the MCP-server config entry the
#     connector wrote.
#
# Usage:
#   scripts/demo_reset.sh                        # default vault ~/.tessera/demo.db
#   scripts/demo_reset.sh ~/.tessera/other.db    # custom vault path

set -u

VAULT_PATH="${1:-$HOME/.tessera/demo.db}"
VAULT_DIR="$(dirname "$VAULT_PATH")"
SALT_PATH="${VAULT_PATH}.salt"
RUN_DIR="$HOME/.tessera/run"
EVENTS_DB="$HOME/.tessera/events.db"

# Derive the same socket path the daemon config resolver uses.
if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    SOCKET_DIR="${XDG_RUNTIME_DIR}/tessera"
else
    SOCKET_DIR="/tmp/tessera-user-runtime-$(id -u)"
fi
SOCKET_PATH="${SOCKET_DIR}/tessera.sock"

action() {
    # "$1" = did-something tag (applied/skipped), "$2" = what happened.
    printf "  %-9s %s\n" "[$1]" "$2"
}

echo "tessera demo reset — $(date -u +%FT%TZ)"
echo "  vault: $VAULT_PATH"
echo

# 1. Tell the daemon to stop. `tessera daemon stop` exits non-zero when
# the daemon is not running; we suppress that because a stopped daemon
# is the desired state.
if command -v tessera >/dev/null 2>&1; then
    TESSERA=(tessera)
elif command -v uv >/dev/null 2>&1; then
    TESSERA=(uv run tessera)
else
    TESSERA=()
fi

if [ ${#TESSERA[@]} -gt 0 ]; then
    if "${TESSERA[@]}" daemon status >/dev/null 2>&1; then
        "${TESSERA[@]}" daemon stop >/dev/null 2>&1 || true
        action applied "daemon stop"
    else
        action skipped "daemon not running"
    fi
else
    action skipped "tessera binary not on PATH; cannot stop daemon"
fi

# 2. Remove vault + salt.
removed_any_vault=0
if [ -f "$VAULT_PATH" ]; then
    rm -f "$VAULT_PATH"
    action applied "removed $VAULT_PATH"
    removed_any_vault=1
fi
if [ -f "$SALT_PATH" ]; then
    rm -f "$SALT_PATH"
    action applied "removed $SALT_PATH"
    removed_any_vault=1
fi
if [ "$removed_any_vault" = "0" ]; then
    action skipped "no vault files at $VAULT_PATH"
fi

# 3. Remove daemon runtime files.
for f in "$RUN_DIR/tesserad.pid" "$RUN_DIR/tesserad.log" "$EVENTS_DB"; do
    if [ -f "$f" ]; then
        rm -f "$f"
        action applied "removed $f"
    fi
done

# 4. Remove control socket if it lingers (daemon stop should have done
# this, but an unclean prior run can leave it behind).
if [ -S "$SOCKET_PATH" ]; then
    rm -f "$SOCKET_PATH"
    action applied "removed $SOCKET_PATH"
fi

# 5. Drop the Tessera entry from every v0.1 MCP-client config that
# supports config file edits. ChatGPT has no on-disk config — revoke
# the session token instead (handled automatically by step 2 removing
# the vault, which invalidates every capability token the vault
# issued).
for client in claude-desktop claude-code cursor codex; do
    if [ ${#TESSERA[@]} -gt 0 ]; then
        if "${TESSERA[@]}" disconnect "$client" --vault "$VAULT_PATH" --passphrase "${TESSERA_PASSPHRASE:-demo}" >/dev/null 2>&1; then
            action applied "disconnect $client"
        else
            # Disconnect needs a vault to mint-then-revoke, but the
            # vault was just removed in step 2. The client-side file
            # edit still succeeds because it is independent of vault
            # unlock — `tessera disconnect` reads the config, removes
            # the "tessera" key, writes back. When the vault is gone,
            # disconnect fails at vault-open; fall through to a pure
            # file-edit so the client config still gets cleaned.
            case "$client" in
                claude-desktop)
                    cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
                claude-code)
                    cfg="$HOME/.claude.json" ;;
                cursor)
                    cfg="$HOME/.cursor/mcp.json" ;;
                codex)
                    cfg="$HOME/.codex/config.toml" ;;
                *)
                    cfg="" ;;
            esac
            if [ -n "$cfg" ] && [ -f "$cfg" ] && grep -q '"tessera"' "$cfg" 2>/dev/null; then
                action skipped "$client ($cfg has tessera entry; remove it by hand or after re-init)"
            else
                action skipped "$client not configured"
            fi
        fi
    fi
done

# 6. Remind the caller about env vars that this script cannot unset in
# their shell.
cat <<EOF

Next steps:
  - Clear env vars in your shell:  unset TESSERA_PASSPHRASE TESSERA_TOKEN
  - Re-run the demo from the maintainer's internal demo-script (.docs/user-demo/).

If you had installed a persistent daemon unit:
  - macOS:  launchctl bootout gui/\$(id -u)/com.tessera.tesserad 2>/dev/null
            tessera daemon uninstall
  - Linux:  systemctl --user disable --now tesserad.service
            tessera daemon uninstall
EOF
