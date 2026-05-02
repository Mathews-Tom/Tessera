"""Stdio ↔ HTTP MCP bridge: shape-of-surface unit tests.

End-to-end bridge behaviour (initialize → tools/list → tools/call)
requires an asyncio event loop, two pipes, and a live HTTP upstream;
that's covered by interactive verification during demo recording
and not by these unit tests. This layer asserts the narrower
contracts the bridge is built on:

1. The tool catalogue has the six v0.1 verbs with schemas in place.
2. ``_call_tessera`` unwraps the daemon's ``{"ok": true, "result":
   ...}`` envelope and raises on the error envelope.
3. ``run_stub`` surfaces a loud usage error and exit code 2 (replaces
   the pre-real-bridge structured-refusal stub tests).
"""

from __future__ import annotations

import httpx
import pytest

from tessera.daemon import stdio_bridge


@pytest.mark.unit
def test_tool_catalogue_lists_v0_5_p2_verbs() -> None:
    names = [tool.name for tool in stdio_bridge._TOOLS]
    assert names == [
        "capture",
        "recall",
        "show",
        "list_facets",
        "stats",
        "forget",
        "learn_skill",
        "get_skill",
        "list_skills",
        "resolve_person",
        "list_people",
        "register_agent_profile",
        "get_agent_profile",
        "list_agent_profiles",
    ]
    # Every tool carries a description and an input schema so Claude
    # Desktop's tool picker renders something useful.
    for tool in stdio_bridge._TOOLS:
        assert tool.description
        assert tool.inputSchema["type"] == "object"


@pytest.mark.unit
def test_stub_exits_with_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = stdio_bridge.run_stub()
    assert rc == 2
    err = capsys.readouterr().err
    assert "--url" in err
    assert "--token" in err


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tessera_unwraps_success_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp"
        import json

        body = json.loads(request.read().decode())
        assert body == {"method": "stats", "args": {}}
        return httpx.Response(
            200,
            json={"ok": True, "result": {"facet_count": 42}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await stdio_bridge._call_tessera(client, "http://example/mcp", "stats", {})
    finally:
        await client.aclose()
    assert result == {"facet_count": 42}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tessera_raises_on_error_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"ok": False, "error": "scope_denied"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="scope_denied"):
            await stdio_bridge._call_tessera(
                client, "http://example/mcp", "recall", {"query_text": "x"}
            )
    finally:
        await client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tessera_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "internal"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await stdio_bridge._call_tessera(client, "http://example/mcp", "stats", {})
    finally:
        await client.aclose()
