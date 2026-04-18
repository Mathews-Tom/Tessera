#!/usr/bin/env bash
# Reject imports of HTTP client libraries outside src/tessera/adapters/.
# Per docs/determinism-and-observability.md §CI enforcement (#2).
#
# Adapters have an allow-list because they are the only legitimate outbound
# network surface. Any other source file importing one of these triggers the
# gate. Tests and scripts are excluded — they run in CI only.
set -euo pipefail

FORBIDDEN='^\s*(import|from)\s+(requests|httpx|aiohttp|urllib\.request)\b'

# Paths to scan: src/tessera minus the adapters subtree.
SCAN_ROOT="src/tessera"
ALLOWLIST="src/tessera/adapters"

if [[ ! -d "${SCAN_ROOT}" ]]; then
  echo "no_telemetry_grep: ${SCAN_ROOT} does not exist; skipping"
  exit 0
fi

# grep -REn output format is `<file>:<line>:<content>`; the ^${ALLOWLIST}/
# anchor therefore matches on file-path prefix only. The trailing slash is
# load-bearing — without it, a hypothetical src/tessera/adapters_evil/ tree
# would be excluded by the allowlist by mistake.
offenders=$(grep -REn --include='*.py' "${FORBIDDEN}" "${SCAN_ROOT}" \
  | grep -v "^${ALLOWLIST}/" \
  || true)

if [[ -n "${offenders}" ]]; then
  echo "no-telemetry gate failed; forbidden imports outside ${ALLOWLIST}:"
  echo "${offenders}"
  exit 1
fi

echo "no_telemetry_grep: ok (${SCAN_ROOT} clean)"
