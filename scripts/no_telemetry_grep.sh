#!/usr/bin/env bash
# Reject imports of HTTP client libraries outside src/tessera/adapters/.
# Per docs/determinism-and-observability.md §CI enforcement (#2).
#
# Adapters have an allow-list because they are the only legitimate outbound
# network surface. Any other source file importing one of these triggers the
# gate. Tests and scripts are excluded — they run in CI only.
set -euo pipefail

FORBIDDEN='^\s*(import|from)\s+(requests|httpx|aiohttp|urllib\.request)\b'

# Paths to scan: src/tessera minus the adapters subtree and the
# three CLI/daemon call sites that legitimately need an HTTP client:
#   - src/tessera/cli/_http.py is the shared CLI loopback client to
#     tesserad's HTTP MCP endpoint at 127.0.0.1, used by every
#     subcommand that calls a tool by name (`tessera capture`,
#     `tessera skills list`, `tessera people show`, …). Extracted
#     from tools_cmd.py in the v0.3 People + Skills refactor so the
#     httpx import lives in exactly one place. Calls from CLI to the
#     local daemon do not leave the machine.
#   - src/tessera/daemon/stdio_bridge.py is the stdio-to-HTTP bridge
#     that Claude Desktop launches; it POSTs every tools/list and
#     tools/call to tesserad's /mcp endpoint at 127.0.0.1 over the
#     same loopback path as cli/_http.py. The bridge never reaches a
#     non-local host — `tessera connect claude-desktop` wires it to
#     `http://127.0.0.1:<port>/mcp`.
#   - src/tessera/cli/curl_cmd.py is the recipe builder for the new
#     /api/v1/* REST surface. It executes the printed curl recipe via
#     httpx so users can verify the recipe before wiring it into a
#     hook script, hitting only `$TESSERA_DAEMON_URL` (default
#     `http://127.0.0.1:5710`). Same loopback shape as cli/_http.py
#     and stdio_bridge.py; --print mode skips the HTTP call entirely.
# Extending this list requires a matching §CI enforcement note in
# docs/determinism-and-observability.md.
SCAN_ROOT="src/tessera"
ALLOWLIST="src/tessera/adapters"
ALLOWLISTED_FILES=(
  "src/tessera/cli/_http.py"
  "src/tessera/cli/curl_cmd.py"
  "src/tessera/daemon/stdio_bridge.py"
)

if [[ ! -d "${SCAN_ROOT}" ]]; then
  echo "no_telemetry_grep: ${SCAN_ROOT} does not exist; skipping"
  exit 0
fi

# grep -REn output format is `<file>:<line>:<content>`; the ^${ALLOWLIST}/
# anchor therefore matches on file-path prefix only. The trailing slash is
# load-bearing — without it, a hypothetical src/tessera/adapters_evil/ tree
# would be excluded by the allowlist by mistake.
raw_offenders=$(grep -REn --include='*.py' "${FORBIDDEN}" "${SCAN_ROOT}" \
  | grep -v "^${ALLOWLIST}/" \
  || true)

offenders=""
while IFS= read -r line; do
  [[ -z "${line}" ]] && continue
  skip=0
  for allowed in "${ALLOWLISTED_FILES[@]}"; do
    if [[ "${line}" == "${allowed}:"* ]]; then
      skip=1
      break
    fi
  done
  if [[ "${skip}" -eq 0 ]]; then
    offenders+="${line}"$'\n'
  fi
done <<< "${raw_offenders}"
offenders="${offenders%$'\n'}"

if [[ -n "${offenders}" ]]; then
  echo "no-telemetry gate failed; forbidden imports outside ${ALLOWLIST}:"
  echo "${offenders}"
  exit 1
fi

echo "no_telemetry_grep: ok (${SCAN_ROOT} clean)"
