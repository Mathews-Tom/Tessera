"""``tessera {capture,recall,show,list,stats,forget}`` — MCP tool passthrough.

Each command opens an HTTP MCP request to the running daemon using an
``Authorization: Bearer <token>`` header supplied via --token or the
``TESSERA_TOKEN`` env var. Responses are rendered as indented JSON
(``json.dumps(..., indent=2)``); a tabular / plain-text renderer for
``list`` and ``stats`` is deferred to v0.1.x.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx

from tessera.cli._common import fail
from tessera.cli._ui import EMOJI, console, report_table, status, success
from tessera.daemon.config import DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT
from tessera.vault.facets import WRITABLE_FACET_TYPES


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    capture = subparsers.add_parser("capture", help="capture a facet")
    _add_http_args(capture)
    capture.add_argument("content")
    # ``--facet-type`` is required. There is no single sensible default —
    # every facet type is an explicit user choice across the v0.3
    # writable vocabulary (identity / preference / workflow / project /
    # style / person / skill).
    capture.add_argument(
        "--facet-type",
        required=True,
        choices=sorted(WRITABLE_FACET_TYPES),
        help="one of the writable facet types",
    )
    capture.add_argument("--source-tool", default=None)
    capture.set_defaults(handler=_cmd_capture)

    recall = subparsers.add_parser("recall", help="hybrid recall")
    _add_http_args(recall)
    recall.add_argument("query")
    recall.add_argument("-k", type=int, default=10)
    recall.add_argument("--facet-types", default=None, help="comma-separated")
    recall.set_defaults(handler=_cmd_recall)

    show = subparsers.add_parser("show", help="show one facet by external_id")
    _add_http_args(show)
    show.add_argument("external_id")
    show.set_defaults(handler=_cmd_show)

    stats = subparsers.add_parser("stats", help="vault stats")
    _add_http_args(stats)
    stats.set_defaults(handler=_cmd_stats)

    forget = subparsers.add_parser("forget", help="soft-delete one facet by external_id")
    _add_http_args(forget)
    forget.add_argument("external_id")
    forget.add_argument("--reason", default=None)
    forget.set_defaults(handler=_cmd_forget)


def _add_http_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument(
        "--token",
        default=None,
        help="bearer token; default is $TESSERA_TOKEN",
    )


def _resolve_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("TESSERA_TOKEN")
    if not token:
        raise SystemExit("access token required; pass --token or export TESSERA_TOKEN")
    return token


def _call(args: argparse.Namespace, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = _resolve_token(args)
    url = f"http://{args.host}:{args.port}/mcp"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"method": method, "args": payload},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(f"daemon unreachable at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise SystemExit(f"HTTP {resp.status_code}: {resp.text.strip()}")
    body = resp.json()
    if not body.get("ok"):
        raise SystemExit(f"error: {body.get('error', 'unknown')}")
    result = body.get("result", {})
    if not isinstance(result, dict):
        raise SystemExit("malformed response: result is not an object")
    return result


def _print_json(result: dict[str, Any]) -> None:
    """Render an MCP response as indented JSON with Rich syntax highlighting on TTY.

    The actual bytes on a pipe are still ``json.dumps(..., indent=2)`` so
    downstream ``| jq`` keeps working; only the TTY path gets colour.
    """

    from rich.syntax import Syntax

    payload = json.dumps(result, indent=2)
    if console.is_terminal:
        console.print(Syntax(payload, "json", theme="ansi_dark", background_color="default"))
    else:
        console.print(payload, markup=False, highlight=False)


def _cmd_capture(args: argparse.Namespace) -> int:
    payload = {"content": args.content, "facet_type": args.facet_type}
    if args.source_tool:
        payload["source_tool"] = args.source_tool
    with status(f"capturing {args.facet_type} facet", emoji=EMOJI["capture"]):
        try:
            result = _call(args, "capture", payload)
        except SystemExit as exc:
            return fail(str(exc))
    _print_json(result)
    success(f"captured {args.facet_type}", emoji=EMOJI["capture"])
    return 0


def _cmd_recall(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"query_text": args.query, "k": args.k}
    if args.facet_types:
        payload["facet_types"] = [t.strip() for t in args.facet_types.split(",")]
    with status(f"recall (k={args.k})", emoji=EMOJI["recall"]):
        try:
            result = _call(args, "recall", payload)
        except SystemExit as exc:
            return fail(str(exc))
    matches = result.get("matches")
    if isinstance(matches, list) and console.is_terminal:
        table = report_table(
            f"recall results (query={args.query!r})",
            ["rank", "facet_type", "score", "external_id", "snippet"],
            emoji=EMOJI["recall"],
        )
        for i, m in enumerate(matches):
            if not isinstance(m, dict):
                continue
            table.add_row(
                str(i + 1),
                str(m.get("facet_type", "")),
                f"{m.get('score', 0):.3f}" if isinstance(m.get("score"), int | float) else "",
                str(m.get("external_id", "")),
                str(m.get("snippet", ""))[:80],
            )
        console.print(table)
    else:
        _print_json(result)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with status(f"show {args.external_id}", emoji=EMOJI["recall"]):
        try:
            result = _call(args, "show", {"external_id": args.external_id})
        except SystemExit as exc:
            return fail(str(exc))
    _print_json(result)
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with status("gathering vault stats", emoji=EMOJI["vault"]):
        try:
            result = _call(args, "stats", {})
        except SystemExit as exc:
            return fail(str(exc))
    by_type = result.get("by_facet_type")
    if isinstance(by_type, dict) and console.is_terminal:
        table = report_table("vault stats", ["facet_type", "count"], emoji=EMOJI["vault"])
        for k in sorted(by_type.keys()):
            table.add_row(str(k), str(by_type[k]))
        console.print(table)
        # Render any extra top-level fields (totals, vault_id) below the table.
        extras = {k: v for k, v in result.items() if k != "by_facet_type"}
        if extras:
            _print_json(extras)
    else:
        _print_json(result)
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"external_id": args.external_id}
    if args.reason:
        payload["reason"] = args.reason
    with status(f"forgetting {args.external_id}", emoji=EMOJI["forget"]):
        try:
            result = _call(args, "forget", payload)
        except SystemExit as exc:
            return fail(str(exc))
    _print_json(result)
    success(f"forgot {args.external_id}", emoji=EMOJI["forget"])
    return 0
