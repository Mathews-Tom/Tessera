"""``tessera {capture,recall,show,stats}`` — MCP tool passthrough.

Each command opens an HTTP MCP request to the running daemon using an
``Authorization: Bearer <token>`` header supplied via --token or the
``TESSERA_TOKEN`` env var. Responses are rendered as plain text for
readability; JSON output is an intentional v0.1.x followup rather
than a v0.1 exit-gate requirement.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx

from tessera.cli._common import fail
from tessera.daemon.config import DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT
from tessera.vault.facets import V0_1_FACET_TYPES


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    capture = subparsers.add_parser("capture", help="capture a facet")
    _add_http_args(capture)
    capture.add_argument("content")
    # ``--facet-type`` is required. Under the post-reframe five-facet
    # model (ADR 0010) there is no single sensible default — every
    # facet type is an explicit user choice between identity /
    # preference / workflow / project / style.
    capture.add_argument(
        "--facet-type",
        required=True,
        choices=sorted(V0_1_FACET_TYPES),
        help="one of the five v0.1 facet types",
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


def _cmd_capture(args: argparse.Namespace) -> int:
    payload = {"content": args.content, "facet_type": args.facet_type}
    if args.source_tool:
        payload["source_tool"] = args.source_tool
    try:
        result = _call(args, "capture", payload)
    except SystemExit as exc:
        return fail(str(exc))
    print(json.dumps(result, indent=2))
    return 0


def _cmd_recall(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"query_text": args.query, "k": args.k}
    if args.facet_types:
        payload["facet_types"] = [t.strip() for t in args.facet_types.split(",")]
    try:
        result = _call(args, "recall", payload)
    except SystemExit as exc:
        return fail(str(exc))
    print(json.dumps(result, indent=2))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        result = _call(args, "show", {"external_id": args.external_id})
    except SystemExit as exc:
        return fail(str(exc))
    print(json.dumps(result, indent=2))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    try:
        result = _call(args, "stats", {})
    except SystemExit as exc:
        return fail(str(exc))
    print(json.dumps(result, indent=2))
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"external_id": args.external_id}
    if args.reason:
        payload["reason"] = args.reason
    try:
        result = _call(args, "forget", payload)
    except SystemExit as exc:
        return fail(str(exc))
    print(json.dumps(result, indent=2))
    return 0
