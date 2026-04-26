"""``tessera curl`` — print or execute canonical REST recipes.

The point of this subcommand is to make the ``/api/v1/*`` surface
copy-pasteable into hook scripts. ``--print`` emits the literal curl
invocation a user can drop into a Claude Code hook, a shell script, or
a Makefile; the default mode executes the same call via ``httpx`` and
prints the JSON response so the user can verify the recipe before
wiring it.

Token resolution: ``--token`` flag → ``$TESSERA_TOKEN`` → loud error.
URL resolution: ``--url`` flag → ``$TESSERA_DAEMON_URL`` → default
``http://127.0.0.1:5710``.

This module deliberately does not own any retry, circuit-breaker, or
auto-mint logic — those belong in the hook author's script. ``tessera
curl`` is a recipe builder, not a client library.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from tessera.cli._common import CliError, fail
from tessera.cli._ui import error as _ui_error

_DEFAULT_DAEMON_URL = "http://127.0.0.1:5710"


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "curl",
        help="print or execute canonical curl recipes for /api/v1/*",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the curl command without executing it (copy-paste mode)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="bearer token; default $TESSERA_TOKEN",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="daemon base URL; default $TESSERA_DAEMON_URL or http://127.0.0.1:5710",
    )
    sub = parser.add_subparsers(dest="curl_verb", required=True)

    recall = sub.add_parser("recall", help="GET /api/v1/recall")
    recall.add_argument("query", help="natural-language query text")
    recall.add_argument("--k", type=int, default=None)
    recall.add_argument(
        "--facet-types",
        default=None,
        help="comma-separated facet types to fan out over",
    )
    recall.set_defaults(handler=_cmd_recall)

    capture = sub.add_parser("capture", help="POST /api/v1/capture")
    capture.add_argument("content", help="facet content")
    capture.add_argument("--facet-type", required=True)
    capture.add_argument("--source-tool", default=None)
    capture.set_defaults(handler=_cmd_capture)

    show = sub.add_parser("show", help="GET /api/v1/facets/<external_id>")
    show.add_argument("external_id")
    show.set_defaults(handler=_cmd_show)

    forget = sub.add_parser("forget", help="DELETE /api/v1/facets/<external_id>")
    forget.add_argument("external_id")
    forget.add_argument("--reason", default=None)
    forget.set_defaults(handler=_cmd_forget)

    stats = sub.add_parser("stats", help="GET /api/v1/stats")
    stats.set_defaults(handler=_cmd_stats)

    list_facets = sub.add_parser("list-facets", help="GET /api/v1/facets")
    list_facets.add_argument("--facet-type", required=True)
    list_facets.add_argument("--limit", type=int, default=None)
    list_facets.add_argument("--since", type=int, default=None)
    list_facets.set_defaults(handler=_cmd_list_facets)


def _resolve_token(arg_value: str | None) -> str:
    if arg_value:
        return arg_value
    env = os.environ.get("TESSERA_TOKEN")
    if env:
        return env
    raise CliError(
        "no bearer token; pass --token or export TESSERA_TOKEN "
        "(mint via `tessera tokens create --client-name cli --token-class service`)"
    )


def _resolve_url(arg_value: str | None) -> str:
    if arg_value:
        return arg_value.rstrip("/")
    env = os.environ.get("TESSERA_DAEMON_URL")
    if env:
        return env.rstrip("/")
    return _DEFAULT_DAEMON_URL


def _cmd_recall(args: argparse.Namespace) -> int:
    params: dict[str, str] = {"q": args.query}
    if args.k is not None:
        params["k"] = str(args.k)
    if args.facet_types is not None:
        params["facet_types"] = args.facet_types
    return _run_get(args, "/api/v1/recall", params)


def _cmd_capture(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"content": args.content, "facet_type": args.facet_type}
    if args.source_tool is not None:
        body["source_tool"] = args.source_tool
    return _run_post(args, "/api/v1/capture", body)


def _cmd_show(args: argparse.Namespace) -> int:
    return _run_get(args, f"/api/v1/facets/{quote(args.external_id, safe='')}", {})


def _cmd_forget(args: argparse.Namespace) -> int:
    params: dict[str, str] = {}
    if args.reason is not None:
        params["reason"] = args.reason
    return _run_delete(args, f"/api/v1/facets/{quote(args.external_id, safe='')}", params)


def _cmd_stats(args: argparse.Namespace) -> int:
    return _run_get(args, "/api/v1/stats", {})


def _cmd_list_facets(args: argparse.Namespace) -> int:
    params: dict[str, str] = {"facet_type": args.facet_type}
    if args.limit is not None:
        params["limit"] = str(args.limit)
    if args.since is not None:
        params["since"] = str(args.since)
    return _run_get(args, "/api/v1/facets", params)


def _run_get(args: argparse.Namespace, path: str, params: dict[str, str]) -> int:
    try:
        token = _resolve_token(args.token)
    except CliError as exc:
        return fail(str(exc))
    base = _resolve_url(args.url)
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return _print_or_execute(method="GET", url=url, headers=_headers(token), body=None, args=args)


def _run_post(args: argparse.Namespace, path: str, body: dict[str, Any]) -> int:
    try:
        token = _resolve_token(args.token)
    except CliError as exc:
        return fail(str(exc))
    base = _resolve_url(args.url)
    url = f"{base}{path}"
    return _print_or_execute(method="POST", url=url, headers=_headers(token), body=body, args=args)


def _run_delete(args: argparse.Namespace, path: str, params: dict[str, str]) -> int:
    try:
        token = _resolve_token(args.token)
    except CliError as exc:
        return fail(str(exc))
    base = _resolve_url(args.url)
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return _print_or_execute(
        method="DELETE", url=url, headers=_headers(token), body=None, args=args
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _print_or_execute(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    args: argparse.Namespace,
) -> int:
    """Either print the curl command or execute it and print the response.

    The curl rendering uses single-quoted args so a $TOKEN reference in
    the printed string stays unexpanded — the user pastes the literal
    line into their hook script and the shell expands it at run time.
    Execution path uses ``httpx`` directly (the same HTTP client the
    test suite uses) so the output matches what the printed curl would
    produce.
    """

    if args.print_only:
        print(_render_curl(method=method, url=url, headers=headers, body=body))
        return 0
    try:
        with httpx.Client(timeout=30.0) as client:
            if body is not None:
                response = client.request(method, url, json=body, headers=headers)
            else:
                response = client.request(method, url, headers=headers)
    except httpx.HTTPError as exc:
        return fail(f"daemon unreachable at {url}: {type(exc).__name__}: {exc}")
    try:
        payload = response.json()
        rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        rendered = response.text
    print(rendered)
    if response.status_code >= 400:
        _ui_error(f"HTTP {response.status_code}")
        return 1
    return 0


def _render_curl(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> str:
    """Render a copy-pasteable curl invocation.

    Auth header uses ``${TESSERA_TOKEN}`` rather than the literal token
    so the printed line is safe to commit to a hook script — the user's
    shell expands the env var at run time and the actual secret never
    enters disk.
    """

    lines: list[str] = [f"curl -s -X {method} {shlex.quote(url)}"]
    for name, value in headers.items():
        if name.lower() == "authorization":
            lines.append(r'  -H "Authorization: Bearer ${TESSERA_TOKEN}"')
        else:
            lines.append(f"  -H {shlex.quote(f'{name}: {value}')}")
    if body is not None:
        lines.append(f"  -H {shlex.quote('Content-Type: application/json')}")
        lines.append(f"  -d {shlex.quote(json.dumps(body, ensure_ascii=False))}")
    return " \\\n".join(lines)


__all__ = ["register"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="tessera curl")
    sub = parser.add_subparsers(dest="command")
    register(sub)
    args = parser.parse_args()
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        sys.exit(2)
    sys.exit(handler(args))
