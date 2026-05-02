#!/usr/bin/env python3
"""ADR 0021 §canonical_json — determinism gate.

Run the project-local canonicalizer against a fixed input vector
twice and assert the byte output is identical. The vector covers
every shape the audit chain needs to round-trip: integers, floats,
booleans, nulls, ASCII strings, non-ASCII strings, control
characters, surrogate pairs, datetimes, nested objects, and lists.

Exit 0 on byte-stable output; exit 1 on drift. Wired into the
``audit-chain-determinism`` CI job; runs on every PR that touches
``src/tessera/vault/canonical_json.py``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from tessera.vault.canonical_json import canonical_json

_FIXED_VECTOR = {
    "id": 1,
    "at": datetime(2026, 5, 2, 12, 0, 0, 123456, tzinfo=UTC),
    "actor": "system",
    "agent_id": 7,
    "op": "facet_inserted",
    "target_external_id": "01HZX1Y2Z3MNPQRSTVWXYZ0123",
    "payload": {
        "facet_type": "agent_profile",
        "source_tool": "cli",
        "is_duplicate": False,
        "content_hash_prefix": "deadbeef",
        "volatility": "persistent",
        "ttl_seconds": None,
        "unicode_label": "café 🦊",
        "control_chars": "tab\there\nand newline",
        "negative_int": -42,
        "shortest_float": 1.5,
        "list_of_mixed": [None, True, False, 0, "x"],
    },
}


def main() -> int:
    first = canonical_json(_FIXED_VECTOR)
    second = canonical_json(_FIXED_VECTOR)
    if first != second:
        sys.stderr.write(
            "audit_chain_determinism: canonical_json drift detected\n"
            f"  first  ({len(first)} bytes): {first!r}\n"
            f"  second ({len(second)} bytes): {second!r}\n"
        )
        return 1
    sys.stdout.write(
        f"audit_chain_determinism: ok ({len(first)} bytes; sha256-ready for chain insert)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
