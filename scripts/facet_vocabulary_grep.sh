#!/usr/bin/env bash
# Reject references to retired facet types in src/ and tests/.
#
# Post-reframe (ADR 0010 supersedes ADR 0004) the facet vocabulary is:
#   v0.1 writable: identity | preference | workflow | project | style
#   v0.3 reserved: person | skill
#   v0.5 reserved: compiled_notebook
#
# The pre-reframe types (`episodic`, `semantic`, `relationship`, `goal`,
# `judgment`) no longer appear in schema CHECK, scope allowlists, or the
# capture write path. Tests and new code must not reintroduce them.
#
# The gate matches string literals only (single or double quoted) so
# legitimate uses of the English words `semantic` (as in "semantic
# similarity") and `relationship` (as in prose docstrings) are not
# swept.
#
# Allowed exception: src/tessera/migration/runner.py carries the v1
# forward-migration script, which MUST name the old CHECK values in
# CASE expressions and WHERE clauses to map v1 rows to the v2 schema.

set -euo pipefail

# Quoted-literal regexes for the retired facet types. Anchored to the
# character classes SQLite CHECK expressions, Python string literals,
# and scope JSON lists all use.
FORBIDDEN='("|'\'')(episodic|semantic|relationship|goal|judgment)("|'\'')'

SCAN_ROOTS=(src/tessera tests)
# Files that legitimately reference the retired types:
#   * ``migration/runner.py`` and ``tests/unit/test_migration_runner.py``
#     carry the v1 -> v2 mapping.
#   * The three boundary-validation tests assert that the retired types
#     are *rejected* — they must mention them to do so.
ALLOWLISTED_FILES=(
  "src/tessera/migration/runner.py"
  "tests/unit/test_migration_runner.py"
  "tests/unit/test_vault_schema.py"
  "tests/unit/test_mcp_tool_validation.py"
  "tests/unit/test_cli_network_cmds.py"
)

offenders=""
for root in "${SCAN_ROOTS[@]}"; do
  if [[ ! -d "${root}" ]]; then
    continue
  fi
  raw=$(grep -REn --include='*.py' -E "${FORBIDDEN}" "${root}" || true)
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
  done <<< "${raw}"
done
offenders="${offenders%$'\n'}"

if [[ -n "${offenders}" ]]; then
  echo "facet-vocabulary gate failed; retired facet types referenced outside the migration allowlist:"
  echo "${offenders}"
  exit 1
fi

echo "facet_vocabulary_grep: ok (src/ and tests/ clean)"
