"""ChatGPT Developer Mode connector via one-time URL exchange.

ChatGPT Dev Mode registers external tools through a URL-exchange
handshake: the user pastes a bootstrap URL into ChatGPT, ChatGPT
POSTs the URL's nonce to the Tessera daemon, and the daemon returns
a short-lived session token that ChatGPT then carries on every
subsequent MCP call. The long-lived token never travels through the
URL, and the bootstrap URL self-invalidates on first use or after
30 seconds — whichever comes first.

The flow, per ADR 0007:

1. ``tessera connect chatgpt`` asks the daemon to mint a bootstrap
   nonce. The daemon stores the nonce alongside a pre-minted session
   token (hashed) with a 30-second TTL and a one-time-use flag.
2. The CLI prints a URL shaped like::

       http://127.0.0.1:5710/mcp/exchange?nonce=<nonce>

   plus the instructions to paste it into ChatGPT Dev Mode.
3. ChatGPT POSTs to that URL. The daemon validates the nonce, marks
   it used, and returns ``{"token": "tessera_session_..."}`` in the
   JSON body.
4. ChatGPT uses the returned token on every subsequent MCP call.

The CLI side lives here; the daemon-side exchange endpoint lives in
:mod:`tessera.daemon.http_mcp`.

This connector does not read or write a config file. Unlike the
JSON/TOML connectors, its apply() returns a bootstrap URL the user
must paste by hand. disconnect() revokes the session token via the
tokens surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tessera.connectors.base import (
    ConnectorError,
    ConnectorResult,
    McpServerSpec,
)


@dataclass(frozen=True, slots=True)
class ChatGptConnector:
    """Pass-through connector that reports the bootstrap URL to the caller.

    The caller (``tessera connect chatgpt``) is responsible for
    minting the bootstrap nonce via the control plane and printing
    the URL. This connector exists so the CLI dispatch table stays
    uniform — ``get_connector("chatgpt")`` returns an object with
    the same protocol shape as the JSON / TOML connectors — while
    leaving the URL-exchange ceremony to the CLI layer that owns the
    control-socket handshake.
    """

    client_id: str = "chatgpt"
    display_name: str = "ChatGPT Developer Mode"

    def default_path(self) -> Path:
        # ChatGPT Dev Mode has no on-disk config file on the user's
        # machine — the transport is the URL the user pastes into the
        # ChatGPT UI. Callers asking for a path receive a clear error.
        raise ConnectorError(
            "ChatGPT Developer Mode does not use an on-disk config file; "
            "run 'tessera connect chatgpt' to get a bootstrap URL."
        )

    def apply(self, path: Path, server: McpServerSpec) -> ConnectorResult:
        del path, server
        raise ConnectorError(
            "ChatGPT Developer Mode is connected via the URL-exchange flow; "
            "the CLI handles it directly, not through apply()."
        )

    def remove(self, path: Path) -> ConnectorResult:
        del path
        raise ConnectorError(
            "Revoke the ChatGPT session token with 'tessera tokens revoke', "
            "not 'tessera disconnect chatgpt'."
        )


__all__ = ["ChatGptConnector"]
