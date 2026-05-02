#!/usr/bin/env bash
# ADR 0021 §Insert path — single writer invariant.
#
# Direct ``INSERT INTO audit_log`` from anywhere outside the
# canonical insert path bypasses the chain hash and breaks the
# tamper-evidence claim. The chain insert lives in
# ``src/tessera/vault/audit_chain.py``; the legacy
# ``audit.write`` shim delegates to it. The migration runner's
# backfill writes via ``UPDATE`` (not ``INSERT``) so the chain
# stays append-only at insert time.
#
# Allowed sources:
#   * src/tessera/vault/audit_chain.py — the canonical insert path.
#
# Test files are excluded from the gate — security tests in
# tests/security/test_audit_chain.py intentionally splice forged
# rows via raw INSERT to verify the walker raises.

set -euo pipefail

PATTERN='INSERT INTO audit_log\b'
SCAN_ROOT="src/tessera"
ALLOWLISTED_FILES=(
  "src/tessera/vault/audit_chain.py"
)

if [[ ! -d "${SCAN_ROOT}" ]]; then
  echo "audit_chain_single_writer: ${SCAN_ROOT} does not exist; skipping"
  exit 0
fi

raw_offenders=$(grep -REn --include='*.py' "${PATTERN}" "${SCAN_ROOT}" \
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
  echo "audit-chain-single-writer gate failed; direct INSERT INTO audit_log"
  echo "outside the canonical insert path bypasses the V0.5-P8 hash chain"
  echo "and breaks ADR 0021's tamper-evidence claim."
  echo
  echo "Offenders:"
  echo "${offenders}"
  echo
  echo "Route audit writes through tessera.vault.audit.write or"
  echo "tessera.vault.audit_chain.audit_log_append."
  exit 1
fi

echo "audit_chain_single_writer: ok (${SCAN_ROOT} clean)"
