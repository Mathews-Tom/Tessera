#!/usr/bin/env bash
# Reject references to the retired ``assume_identity`` tool in src/ and tests/.
#
# Per ADR 0010, ``assume_identity`` has been replaced by ``recall`` with
# ``facet_types`` defaulting to every type the caller is scoped for.
# The identity module, its bundle assembler, and the MCP tool
# registration have been removed. Tests and new code must not
# reintroduce the name.
#
# Historical mentions are allowed under ``docs/`` only — the ADRs,
# swcr-spec retirement note, and B-RET-3 rename note reference the
# retired tool by name as part of the supersede record. Those live
# outside the scan roots.

set -euo pipefail

SCAN_ROOTS=(src/tessera tests)

offenders=""
for root in "${SCAN_ROOTS[@]}"; do
  if [[ ! -d "${root}" ]]; then
    continue
  fi
  raw=$(grep -REn --include='*.py' 'assume_identity' "${root}" || true)
  if [[ -n "${raw}" ]]; then
    offenders+="${raw}"$'\n'
  fi
done
offenders="${offenders%$'\n'}"

if [[ -n "${offenders}" ]]; then
  echo "assume-identity gate failed; references to the retired tool:"
  echo "${offenders}"
  exit 1
fi

echo "assume_identity_grep: ok (src/ and tests/ clean)"
