"""REST surface — ``/api/v1/*`` endpoints sharing /mcp's auth + dispatch.

This module is the second transport for the same daemon dispatcher: every
endpoint here translates HTTP method + path + query/body into the ``(method,
args)`` shape that ``_authenticate_and_dispatch`` already understands. The
response is the dispatcher's return dict directly as JSON, with no
``{"ok": true, "result": ...}`` envelope — the URL + status code carry the
success signal that ``ok`` carries on the MCP wire.

Why bypass MCP's envelope: hooks and skills calling the daemon via curl
parse responses with ``jq``, and stripping the envelope cuts ~50-150
tokens per call when the response is fed back into a model context. The
MCP surface stays for clients that auto-discover tools (Claude Desktop,
Cursor, Codex). Both surfaces share auth, scope, and storage paths so
neither becomes a stale mirror of the other.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tessera.auth.tokens import VerifiedCapability

# Re-imported lazily inside the handler to avoid an import cycle through
# http_mcp; rest.py is called from http_mcp's request handler.
_API_PREFIX = "/api/v1/"

Dispatcher = Callable[[VerifiedCapability, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class RestError(Exception):
    """Wire-shape REST failure with HTTP status and machine-readable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _coerce_int(name: str, raw: str) -> int:
    """Parse a query-string int with a clear error path.

    Query-string values arrive as strings. The dispatcher's
    ``_require_int`` rejects non-int values, so the REST layer coerces
    typed fields (``k``, ``limit``, ``since``) before the dispatcher
    sees them. Booleans are not accepted as ints here even though
    Python treats ``bool`` as ``int`` — matching the dispatcher's
    explicit ``isinstance(value, bool)`` rejection.
    """

    try:
        return int(raw)
    except ValueError as exc:
        raise RestError(400, "invalid_input", f"{name} must be an integer") from exc


def _coerce_bool(name: str, raw: str) -> bool:
    """Accept ``true``/``false``/``1``/``0`` (case-insensitive)."""

    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise RestError(400, "invalid_input", f"{name} must be a boolean")


def _parse_json_body(body: bytes) -> dict[str, Any]:
    """Decode a JSON body to a dict; raise a clean RestError on failure."""

    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestError(400, "invalid_input", "invalid json body") from exc
    if not isinstance(payload, dict):
        raise RestError(400, "invalid_input", "body must be a JSON object")
    return payload


def _parse_query(query: str) -> dict[str, list[str]]:
    """Wrap parse_qs so callers do not import urllib themselves."""

    return parse_qs(query, keep_blank_values=True)


def _single(qs: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for ``key`` or None when absent.

    Repeated query params (``?facet_types=a&facet_types=b``) are
    handled separately by the caller; this helper only flattens the
    one-value case.
    """

    values = qs.get(key)
    if not values:
        return None
    return values[0]


def build_args_for_route(
    *,
    http_method: str,
    path: str,
    query: str,
    body: bytes,
) -> tuple[str, dict[str, Any]]:
    """Translate REST request shape into the dispatcher's ``(method, args)``.

    Returns ``(dispatcher_method, args_dict)`` ready for
    ``dispatch_tool_call``. Raises :class:`RestError` for unknown
    routes, malformed bodies, or missing required fields. Method/path
    matching is exhaustive and explicit — a 404 here is preferable to
    a silent dispatch with a typo'd verb.
    """

    qs = _parse_query(query)

    if path == "/api/v1/capture" and http_method == "POST":
        body_args = _parse_json_body(body)
        return "capture", body_args

    if path == "/api/v1/recall" and http_method == "GET":
        query_text = _single(qs, "q") or _single(qs, "query_text")
        if query_text is None:
            raise RestError(400, "invalid_input", "q must be a string")
        args: dict[str, Any] = {"query_text": query_text}
        if (k_raw := _single(qs, "k")) is not None:
            args["k"] = _coerce_int("k", k_raw)
        facet_types = qs.get("facet_types") or qs.get("facet_type")
        if facet_types:
            # Two equivalent input shapes: repeated ?facet_types=a&facet_types=b
            # and a comma-list ?facet_types=a,b. Both flatten to the same
            # tuple the dispatcher expects.
            flattened: list[str] = []
            for value in facet_types:
                flattened.extend(part.strip() for part in value.split(",") if part.strip())
            args["facet_types"] = flattened
        if (budget_raw := _single(qs, "requested_budget_tokens")) is not None:
            args["requested_budget_tokens"] = _coerce_int("requested_budget_tokens", budget_raw)
        return "recall", args

    if path == "/api/v1/stats" and http_method == "GET":
        return "stats", {}

    if path == "/api/v1/facets" and http_method == "GET":
        facet_type = _single(qs, "facet_type")
        if facet_type is None:
            raise RestError(400, "invalid_input", "facet_type must be a string")
        list_args: dict[str, Any] = {"facet_type": facet_type}
        if (limit_raw := _single(qs, "limit")) is not None:
            list_args["limit"] = _coerce_int("limit", limit_raw)
        if (since_raw := _single(qs, "since")) is not None:
            list_args["since"] = _coerce_int("since", since_raw)
        return "list_facets", list_args

    if path.startswith("/api/v1/facets/"):
        external_id = path[len("/api/v1/facets/") :]
        if not external_id or "/" in external_id:
            raise RestError(404, "unknown_method", "unknown route")
        if http_method == "GET":
            return "show", {"external_id": external_id}
        if http_method == "DELETE":
            forget_args: dict[str, Any] = {"external_id": external_id}
            reason = _single(qs, "reason")
            if reason is not None:
                forget_args["reason"] = reason
            return "forget", forget_args

    if path == "/api/v1/skills" and http_method == "POST":
        return "learn_skill", _parse_json_body(body)

    if path == "/api/v1/skills" and http_method == "GET":
        skill_args: dict[str, Any] = {}
        if (active_only_raw := _single(qs, "active_only")) is not None:
            skill_args["active_only"] = _coerce_bool("active_only", active_only_raw)
        if (limit_raw := _single(qs, "limit")) is not None:
            skill_args["limit"] = _coerce_int("limit", limit_raw)
        return "list_skills", skill_args

    if path.startswith("/api/v1/skills/"):
        name = path[len("/api/v1/skills/") :]
        if not name or "/" in name:
            raise RestError(404, "unknown_method", "unknown route")
        if http_method == "GET":
            return "get_skill", {"name": name}

    if path == "/api/v1/people" and http_method == "GET":
        people_args: dict[str, Any] = {}
        if (limit_raw := _single(qs, "limit")) is not None:
            people_args["limit"] = _coerce_int("limit", limit_raw)
        if (since_raw := _single(qs, "since")) is not None:
            people_args["since"] = _coerce_int("since", since_raw)
        return "list_people", people_args

    if path == "/api/v1/people/resolve" and http_method == "GET":
        mention = _single(qs, "mention")
        if mention is None:
            raise RestError(400, "invalid_input", "mention must be a string")
        return "resolve_person", {"mention": mention}

    if path == "/api/v1/agent_profiles" and http_method == "POST":
        return "register_agent_profile", _parse_json_body(body)

    if path == "/api/v1/agent_profiles" and http_method == "GET":
        profile_args: dict[str, Any] = {}
        if (limit_raw := _single(qs, "limit")) is not None:
            profile_args["limit"] = _coerce_int("limit", limit_raw)
        if (since_raw := _single(qs, "since")) is not None:
            profile_args["since"] = _coerce_int("since", since_raw)
        return "list_agent_profiles", profile_args

    if path.startswith("/api/v1/agent_profiles/"):
        suffix = path[len("/api/v1/agent_profiles/") :]
        # Two trailing-slash forms:
        #   /api/v1/agent_profiles/<ulid>          → get_agent_profile
        #   /api/v1/agent_profiles/<ulid>/checklist → list_checks_for_agent
        if suffix.endswith("/checklist"):
            external_id = suffix[: -len("/checklist")]
            if not external_id or "/" in external_id:
                raise RestError(404, "unknown_method", "unknown route")
            if http_method == "GET":
                return "list_checks_for_agent", {"profile_external_id": external_id}
        elif suffix and "/" not in suffix and http_method == "GET":
            return "get_agent_profile", {"external_id": suffix}

    if path == "/api/v1/checklists" and http_method == "POST":
        return "register_checklist", _parse_json_body(body)

    if path == "/api/v1/retrospectives" and http_method == "POST":
        return "record_retrospective", _parse_json_body(body)

    if path == "/api/v1/compiled_artifacts" and http_method == "POST":
        return "register_compiled_artifact", _parse_json_body(body)

    if path.startswith("/api/v1/compiled_artifacts/"):
        external_id = path[len("/api/v1/compiled_artifacts/") :]
        if not external_id or "/" in external_id:
            raise RestError(404, "unknown_method", "unknown route")
        if http_method == "GET":
            return "get_compiled_artifact", {"external_id": external_id}

    if path == "/api/v1/compile_sources" and http_method == "GET":
        target = _single(qs, "target")
        if target is None:
            raise RestError(400, "invalid_input", "target must be a string")
        compile_args: dict[str, Any] = {"target": target}
        if (limit_raw := _single(qs, "limit")) is not None:
            compile_args["limit"] = _coerce_int("limit", limit_raw)
        return "list_compile_sources", compile_args

    raise RestError(404, "unknown_method", "unknown route")


def parse_target(target: str) -> tuple[str, str]:
    """Split ``/api/v1/recall?q=foo`` into (path, query) without urllib globals."""

    parts = urlsplit(target)
    return parts.path, parts.query


def is_rest_route(path: str) -> bool:
    return path.startswith(_API_PREFIX)


__all__ = [
    "Dispatcher",
    "RestError",
    "build_args_for_route",
    "is_rest_route",
    "parse_target",
]
