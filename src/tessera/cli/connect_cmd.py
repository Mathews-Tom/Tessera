"""``tessera connect <client>`` / ``tessera disconnect <client>``.

Mints a capability token for the target client (direct vault access)
and then delegates to the matching
:class:`~tessera.connectors.base.Connector` to write or remove the
MCP entry from the client's config file. ChatGPT Developer Mode is
handled specially: the CLI asks the running daemon to stash the raw
session token under a one-time-use nonce and prints the bootstrap URL
the user pastes into ChatGPT.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_agent_id,
    resolve_passphrase,
)
from tessera.cli._ui import EMOJI, info, kv_panel, status, success
from tessera.cli.tokens_cmd import _resolve_ttl_seconds
from tessera.connectors import (
    Connector,
    UnknownClientError,
    available_clients,
    get_connector,
)
from tessera.connectors.base import McpServerSpec
from tessera.connectors.chatgpt import ChatGptConnector
from tessera.daemon.config import DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT

# Sensible default scopes for a newly-minted client token. Claude
# Desktop / Code / Cursor / Codex / ChatGPT all work against the
# five v0.1 writable facets; read+write wildcarded to them gives a
# new user a connector that "just works" without a scope-tuning
# exercise. Operators who want a narrower grant can still use
# ``tessera tokens create`` + paste the token into the config by
# hand; this command is the one-shot convenience path.
_DEFAULT_READ = ("identity", "preference", "workflow", "project", "style")
_DEFAULT_WRITE = ("preference", "workflow", "project", "style")


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    connect = subparsers.add_parser("connect", help="connect an MCP client to the running tesserad")
    _add_common_args(connect)
    # --agent-id is optional. When omitted, the handler auto-selects
    # the single agent in the vault (the common case after
    # ``tessera init`` creates exactly one default agent). Fails loud
    # on zero or >1 agents so the operator knows why the default is
    # ambiguous. Matches the contract ``tessera tokens create`` uses.
    connect.add_argument(
        "--agent-id",
        type=int,
        default=None,
        help="agent id; defaults to the sole agent when the vault has exactly one",
    )
    connect.add_argument(
        "--url",
        default=f"http://{DEFAULT_HTTP_HOST}:{DEFAULT_HTTP_PORT}/mcp",
        help="HTTP MCP URL the client will POST to",
    )
    connect.add_argument(
        "--token-class",
        choices=["session", "service"],
        default="service",
        help="service tokens are multi-use and long-lived; session tokens expire quickly",
    )
    connect.add_argument(
        "--token-ttl-days",
        type=float,
        default=None,
        help=(
            "override the class-default access-token TTL (capped at 90 days). "
            "Use for demo installs that need 'set and forget' — with the "
            "default service TTL of 24h, the client config needs to be "
            "refreshed daily via another `tessera connect` run. Set to e.g. "
            "30 for a month-long token."
        ),
    )
    connect.add_argument(
        "--path",
        type=Path,
        default=None,
        help="override the client's default config path (ChatGPT has no file)",
    )
    connect.set_defaults(handler=_cmd_connect)

    disconnect = subparsers.add_parser(
        "disconnect", help="remove the Tessera entry from a client's MCP config"
    )
    _add_common_args(disconnect)
    disconnect.add_argument(
        "--path",
        type=Path,
        default=None,
        help="override the client's default config path",
    )
    disconnect.set_defaults(handler=_cmd_disconnect)


# ``all`` expands to every file-based client. ChatGPT stays out of the
# expansion because its handler path is the URL-exchange flow (needs a
# running daemon + user-interactive paste), not a config-file write.
_ALL_FILE_BASED_CLIENTS = ("claude-desktop", "claude-code", "cursor", "codex")
_ALL_META = "all"


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "client",
        nargs="+",
        choices=[*available_clients(), _ALL_META],
        help=(
            "one or more client ids; pass multiple (e.g. "
            "`claude-desktop claude-code cursor codex`) or `all` as a "
            "sugar for every file-based client (all except chatgpt)"
        ),
    )
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--passphrase", default=None)
    parser.add_argument("--socket", type=Path, default=None, help="daemon control socket")


def _expand_clients(raw: list[str]) -> list[str]:
    """Expand the ``all`` meta into the file-based clients; dedup order.

    Preserves the caller's order otherwise so the output matches what
    the user typed. ``all`` inserted at any position expands in place;
    duplicates collapse to the first occurrence.
    """

    seen: set[str] = set()
    resolved: list[str] = []
    for entry in raw:
        if entry == _ALL_META:
            for client in _ALL_FILE_BASED_CLIENTS:
                if client not in seen:
                    seen.add(client)
                    resolved.append(client)
            continue
        if entry not in seen:
            seen.add(entry)
            resolved.append(entry)
    return resolved


def _cmd_connect(args: argparse.Namespace) -> int:
    clients = _expand_clients(args.client)
    overall = 0
    for client_id in clients:
        try:
            connector = get_connector(client_id)
        except UnknownClientError as exc:
            fail(str(exc))
            overall = 1
            continue
        if isinstance(connector, ChatGptConnector):
            rc = _connect_chatgpt(args)
        else:
            rc = _connect_file_based(args, connector)
        if rc != 0:
            overall = rc
    return overall


def _cmd_disconnect(args: argparse.Namespace) -> int:
    clients = _expand_clients(args.client)
    overall = 0
    for client_id in clients:
        rc = _disconnect_one(args, client_id)
        if rc != 0:
            overall = rc
    return overall


def _disconnect_one(args: argparse.Namespace, client_id: str) -> int:
    try:
        connector = get_connector(client_id)
    except UnknownClientError as exc:
        return fail(str(exc))
    if isinstance(connector, ChatGptConnector):
        info(
            "ChatGPT Developer Mode has no on-disk config. "
            "Revoke the session token with `tessera tokens revoke --token-id <id>` "
            "— this is the only way to cut access.",
            emoji=EMOJI["connect"],
        )
        return 0
    try:
        path = args.path or connector.default_path()
    except Exception as exc:
        return fail(f"{connector.display_name}: {exc}")
    result = connector.remove(path)
    if result.no_op:
        info(f"{connector.display_name}: no Tessera entry at {result.path}", emoji=EMOJI["connect"])
        return 0
    success(
        f"{connector.display_name}: removed Tessera entry from {result.path}",
        emoji=EMOJI["forget"],
    )
    if result.backup_path is not None:
        info(f"backup: {result.backup_path}")
    return 0


def _connect_file_based(args: argparse.Namespace, connector: Connector) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    access_ttl_seconds = _resolve_ttl_seconds(args.token_ttl_days)
    with status(f"minting token + writing {connector.display_name} config", emoji=EMOJI["connect"]):
        try:
            raw_token = _mint_token(
                vault=args.vault,
                passphrase=passphrase,
                agent_id=args.agent_id,
                client_id=connector.client_id,
                token_class=args.token_class,
                access_ttl_seconds=access_ttl_seconds,
            )
        except Exception as exc:
            return fail(f"token mint failed: {exc}")
        try:
            path = args.path or connector.default_path()
        except Exception as exc:
            return fail(f"{connector.display_name}: {exc}")
        spec = McpServerSpec(url=args.url, token=raw_token)
        try:
            result = connector.apply(path, spec)
        except Exception as exc:
            return fail(f"{connector.display_name}: config write failed: {exc}")
    if result.no_op:
        info(
            f"{connector.display_name}: config already has the Tessera entry at {result.path}",
            emoji=EMOJI["connect"],
        )
    else:
        success(
            f"{connector.display_name}: wrote Tessera entry to {result.path}",
            emoji=EMOJI["connect"],
        )
        if result.backup_path is not None:
            info(f"backup: {result.backup_path}")
    info(f"restart {connector.display_name} to pick up the new MCP server.")
    return 0


def _connect_chatgpt(args: argparse.Namespace) -> int:
    """Report that ChatGPT Developer Mode is a v0.1.x follow-up, with rationale.

    Three stacked blockers surfaced when wiring the "New App (Beta)"
    flow during P14 recording prep:

    1. **HTTPS required.** ChatGPT Developer Mode rejects
       ``http://127.0.0.1:...`` URLs with "Unsafe URL". v0.1 does not
       ship a TLS front for the local daemon.
    2. **No Bearer auth mode.** The dialog's Authentication dropdown
       offers only ``OAuth``, ``Mixed``, ``No Auth``. Tessera's
       capability-token model is not OAuth; Bearer-in-URL is blocked
       by the HTTPS check above.
    3. **Protocol mismatch.** ChatGPT's MCP client speaks canonical
       MCP JSON-RPC 2.0 (``initialize``, ``tools/list``, ``tools/call``).
       Tessera's ``/mcp`` endpoint speaks a custom ``{"method": X,
       "args": Y}`` envelope. The same gap ``tessera stdio`` closes
       for Claude Desktop's stdio side needs a server-side
       equivalent for ChatGPT's HTTP side.

    All three close in v0.1.x. Until they do, the T-shape demo uses
    **Claude Code** as the recall-side client.

    We still mint a token on the v0.1 invocation so the infrastructure
    is warmed up and the user has a token in hand for the day the
    integration lands. The token is printed alongside the paste-values
    that *would* go into "New App" so a user retrying against a future
    daemon that speaks proper MCP over HTTPS has everything ready.
    """

    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    access_ttl_seconds = _resolve_ttl_seconds(args.token_ttl_days)
    try:
        raw_token = _mint_token(
            vault=args.vault,
            passphrase=passphrase,
            agent_id=args.agent_id,
            client_id="chatgpt",
            token_class=args.token_class,
            access_ttl_seconds=access_ttl_seconds,
        )
    except Exception as exc:
        return fail(f"token mint failed: {exc}")

    mcp_url = f"http://{DEFAULT_HTTP_HOST}:{DEFAULT_HTTP_PORT}/mcp"
    kv_panel(
        "ChatGPT Developer Mode — v0.1.x follow-up (values printed for future use)",
        {
            "Name": "Tessera",
            "MCP Server URL": mcp_url,
            "Bearer token": raw_token,
            "Status": "deferred to v0.1.x — see notes below",
        },
        emoji=EMOJI["connect"],
    )
    info("ChatGPT Developer Mode is not yet consumable by Tessera v0.1. Three blockers:")
    info(
        "  1) HTTPS required — http://127.0.0.1:... rejected as 'Unsafe URL'. "
        "Needs a TLS front (mkcert-backed local CA or a tunnel like ngrok)."
    )
    info(
        "  2) No Bearer auth mode in the 'New App' dropdown (only OAuth / Mixed / "
        "No Auth). Tessera's capability-token model is not OAuth."
    )
    info(
        "  3) Protocol mismatch — ChatGPT speaks canonical MCP JSON-RPC 2.0; "
        "Tessera's /mcp speaks a custom {method, args} envelope. Needs a "
        "server-side bridge equivalent to `tessera stdio`."
    )
    info("For the v0.1 T-shape demo, use Claude Code as the recall-side client instead.")
    return 0


def _mint_token(
    *,
    vault: Path,
    passphrase: bytearray,
    agent_id: int | None,
    client_id: str,
    token_class: str,
    access_ttl_seconds: int | None = None,
) -> str:
    """Mint a capability token for ``client_id`` against the vault's agent.

    When ``agent_id`` is None, :func:`resolve_agent_id` auto-selects the
    sole agent in the vault. The resolution happens inside the vault
    unlock so the caller does not have to open the vault twice.

    ``access_ttl_seconds`` overrides the token-class default TTL; pass
    None to keep the class default. Validation lives inside
    :func:`tokens.issue`, which caps at 90 days.
    """

    now_epoch = int(datetime.now(UTC).timestamp())
    scope = build_scope(read=list(_DEFAULT_READ), write=list(_DEFAULT_WRITE))
    with open_vault(vault, passphrase) as vc:
        resolved_agent_id = resolve_agent_id(vc.connection, agent_id)
        issued = tokens.issue(
            vc.connection,
            agent_id=resolved_agent_id,
            client_name=client_id,
            token_class=token_class,  # type: ignore[arg-type]
            scope=scope,
            now_epoch=now_epoch,
            access_ttl_seconds=access_ttl_seconds,
        )
    return issued.raw_token


__all__ = ["register"]
