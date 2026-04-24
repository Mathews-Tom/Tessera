#!/usr/bin/env bash
# Pre-recording environment-readiness probe for the T-shape demo.
#
# Runs in under ~30 seconds against a throwaway vault and prints a
# green/red summary. A green run means the local machine is ready to
# record the demo without pausing on-camera to fix setup.
#
# Usage:
#   scripts/demo_smoke.sh                      # uses a temp vault, cleaned on exit
#   scripts/demo_smoke.sh /path/to/vault.db    # uses the named vault (created if absent)
#
# Exit codes:
#   0  all checks green
#   1  a check failed; see stderr for the first failure reason
#
# What the script does NOT do:
# - Record video (see the internal demo kit under .docs/user-demo/, not tracked)
# - Start or configure MCP clients (see the internal demo kit)
# - Test against a real Ollama model download (only pings /api/tags)

set -euo pipefail

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

pass() {
    echo "  ok: $1"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

VAULT_PATH="${1:-}"
CLEANUP_VAULT=0
if [ -z "$VAULT_PATH" ]; then
    TMPDIR_VAULT="$(mktemp -d -t tessera-smoke-XXXXXX)"
    VAULT_PATH="$TMPDIR_VAULT/vault.db"
    CLEANUP_VAULT=1
fi
export TESSERA_PASSPHRASE="${TESSERA_PASSPHRASE:-demo-smoke}"

cleanup() {
    # Stop any daemon we may have started.
    if [ -n "${DAEMON_PID:-}" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    if [ "$CLEANUP_VAULT" = "1" ] && [ -n "${TMPDIR_VAULT:-}" ]; then
        rm -rf "$TMPDIR_VAULT"
    fi
}
trap cleanup EXIT

echo "tessera demo smoke — $(date -u +%FT%TZ)"
echo "  vault:      $VAULT_PATH"
echo

# 1. Binary present on PATH or via uv.
if command -v tessera >/dev/null 2>&1; then
    TESSERA=(tessera)
elif command -v uv >/dev/null 2>&1; then
    TESSERA=(uv run tessera)
else
    fail "neither 'tessera' nor 'uv' on PATH; install via 'pip install tessera-context' or 'uv sync'"
fi
pass "tessera binary: ${TESSERA[*]}"

# 2. Ollama reachable and nomic-embed-text pulled.
if ! curl -fs --max-time 2 http://localhost:11434/api/tags >/tmp/tessera-smoke-tags.$$.json 2>/dev/null; then
    fail "Ollama not reachable at http://localhost:11434 — run 'ollama serve' first"
fi
if ! grep -q '"nomic-embed-text' /tmp/tessera-smoke-tags.$$.json; then
    fail "ollama has no nomic-embed-text model — run 'ollama pull nomic-embed-text'"
fi
rm -f /tmp/tessera-smoke-tags.$$.json
pass "ollama reachable + nomic-embed-text available"

# 3. Bootstrap the vault + register the active embedding model.
if [ ! -f "$VAULT_PATH" ]; then
    "${TESSERA[@]}" init --vault "$VAULT_PATH" --passphrase "$TESSERA_PASSPHRASE" >/dev/null
    "${TESSERA[@]}" models set \
        --vault "$VAULT_PATH" \
        --passphrase "$TESSERA_PASSPHRASE" \
        --name ollama \
        --model nomic-embed-text \
        --dim 768 \
        --activate >/dev/null
    pass "vault bootstrapped + active model registered"
else
    pass "reusing existing vault at $VAULT_PATH"
fi

# 4. doctor (CLI form — runs without the daemon).
DOCTOR_OUT="$("${TESSERA[@]}" doctor --vault "$VAULT_PATH" --passphrase "$TESSERA_PASSPHRASE" 2>&1 || true)"
if echo "$DOCTOR_OUT" | grep -qiE "^error|\[error\]"; then
    echo "$DOCTOR_OUT" >&2
    fail "tessera doctor reported ERROR"
fi
pass "tessera doctor (no ERROR rows)"

# 5. Start the daemon briefly, confirm control-plane status round-trip.
LOG_FILE="$(mktemp -t tessera-smoke-daemon-XXXXXX.log)"
"${TESSERA[@]}" daemon start-fg \
    --vault "$VAULT_PATH" \
    --passphrase "$TESSERA_PASSPHRASE" \
    >"$LOG_FILE" 2>&1 &
DAEMON_PID=$!

# Wait for daemon ready.
ATTEMPTS=20
while ! "${TESSERA[@]}" daemon status 2>/dev/null | grep -q "vault_id"; do
    ATTEMPTS=$((ATTEMPTS - 1))
    if [ "$ATTEMPTS" -le 0 ]; then
        echo "---- daemon log ----" >&2
        cat "$LOG_FILE" >&2
        fail "daemon did not become ready within ~10 s"
    fi
    sleep 0.5
done
pass "daemon start + status round-trip"

# Capture + recall via the MCP dispatch layer is exercised by the
# integration test suite (tests/integration/test_mcp_tool_surface.py);
# the smoke script deliberately stops at the control-plane boundary to
# stay under 30 s wall time and not pull the cross-encoder weights.

# 6. Stop daemon.
"${TESSERA[@]}" daemon stop >/dev/null 2>&1 || true
# Give the process a beat to close the socket before trap-cleanup fires.
for _ in 1 2 3 4 5; do
    kill -0 "$DAEMON_PID" 2>/dev/null || break
    sleep 0.5
done
pass "daemon stop"

echo
echo "OK — environment ready for demo recording."
echo "Next: follow the maintainer's internal demo-script walkthrough (.docs/user-demo/)."
