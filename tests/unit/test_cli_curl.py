"""``tessera curl`` recipe rendering + token/url resolution.

Execution against a live daemon is exercised by the REST integration
suite. These tests pin the wire-shape contract: the printed curl
command keeps the bearer token as a literal ``${TESSERA_TOKEN}``
reference (so users can paste recipes into hook scripts without leaking
secrets), URL/token resolution honours the same flag → env → default
chain as the rest of the CLI, and parser registration covers every
documented verb.
"""

from __future__ import annotations

import argparse

import pytest

from tessera.cli import curl_cmd
from tessera.cli._common import CliError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tessera")
    sub = parser.add_subparsers(dest="command")
    curl_cmd.register(sub)
    return parser


@pytest.mark.unit
def test_curl_subcommand_registers_every_verb() -> None:
    parser = _build_parser()
    bare = parser.parse_args(["curl", "stats"])
    assert bare.curl_verb == "stats"
    parser.parse_args(["curl", "recall", "hello", "--k", "5"])
    parser.parse_args(["curl", "capture", "content", "--facet-type", "style"])
    parser.parse_args(["curl", "show", "01EXTERNAL"])
    parser.parse_args(["curl", "forget", "01EXTERNAL", "--reason", "rotated"])
    parser.parse_args(["curl", "list-facets", "--facet-type", "style"])


@pytest.mark.unit
def test_resolve_token_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERA_TOKEN", "from-env")
    assert curl_cmd._resolve_token("from-arg") == "from-arg"
    assert curl_cmd._resolve_token(None) == "from-env"
    monkeypatch.delenv("TESSERA_TOKEN")
    with pytest.raises(CliError) as exc_info:
        curl_cmd._resolve_token(None)
    msg = str(exc_info.value)
    assert "TESSERA_TOKEN" in msg
    assert "tessera tokens create" in msg


@pytest.mark.unit
def test_resolve_url_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERA_DAEMON_URL", "http://example.test:9000")
    assert curl_cmd._resolve_url("http://flag.test:8000") == "http://flag.test:8000"
    assert curl_cmd._resolve_url(None) == "http://example.test:9000"
    monkeypatch.delenv("TESSERA_DAEMON_URL")
    assert curl_cmd._resolve_url(None) == "http://127.0.0.1:5710"


@pytest.mark.unit
def test_render_curl_get_keeps_token_unexpanded() -> None:
    rendered = curl_cmd._render_curl(
        method="GET",
        url="http://127.0.0.1:5710/api/v1/recall?q=hello&k=5",
        headers={"Authorization": "Bearer real-secret-value"},
        body=None,
    )
    # The literal token must NEVER appear in the printed recipe — users
    # paste these into hook scripts that may end up in version control.
    assert "real-secret-value" not in rendered
    assert "${TESSERA_TOKEN}" in rendered
    assert "curl -s -X GET" in rendered
    assert "http://127.0.0.1:5710/api/v1/recall?q=hello&k=5" in rendered


@pytest.mark.unit
def test_render_curl_post_includes_body_and_content_type() -> None:
    rendered = curl_cmd._render_curl(
        method="POST",
        url="http://127.0.0.1:5710/api/v1/capture",
        headers={"Authorization": "Bearer xyz"},
        body={"content": "voice sample", "facet_type": "style"},
    )
    assert "-X POST" in rendered
    assert "Content-Type: application/json" in rendered
    # Body is shlex-quoted as a single -d argument.
    assert "voice sample" in rendered
    assert "facet_type" in rendered
