"""Stdio MCP bridge — stub.

Full implementation is P14 v0.1.x; this stub ships the wire shape and
a one-line hello/error response so downstream client configurations
(Claude Desktop's stdio MCP transport) can point at ``tessera stdio``
and receive a structured refusal rather than a silent EOF until the
real bridge lands.

The shape mirrors the HTTP bridge: one JSON request per line on stdin,
one JSON response per line on stdout. The stub reads a single request,
responds ``{"ok": false, "error": "stdio bridge not implemented at v0.1"}``,
and exits.
"""

from __future__ import annotations

import json
import sys


def run_stub() -> int:
    """Read one line from stdin, emit a structured refusal, exit."""

    line = sys.stdin.readline()
    if not line:
        return 0
    response = {"ok": False, "error": "stdio bridge not implemented at v0.1"}
    try:
        json.loads(line)
    except json.JSONDecodeError:
        response["error"] = "stdio bridge not implemented at v0.1 (malformed request)"
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
    return 0


__all__ = ["run_stub"]
