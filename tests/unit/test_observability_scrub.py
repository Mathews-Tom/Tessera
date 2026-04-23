"""Scrubber rejection rules.

Pinned because the scrubber is the last line of defense against a
leaked diagnostic bundle. Every rule gets both a positive (fires on
the violating payload) and negative (passes clean payload) test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.observability.scrub import (
    CONTENT_STRING_LIMIT,
    ScrubberViolationError,
    scrub_bundle_file,
    scrub_jsonl_file,
    scrub_text_file,
)


@pytest.mark.unit
def test_clean_payload_passes() -> None:
    scrub_bundle_file("env.json", {"os": "Darwin", "cpu_count": 10})


@pytest.mark.unit
@pytest.mark.parametrize(
    "forbidden_key",
    [
        "token",
        "api_key",
        "passphrase",
        "secret_sauce",
        "bearer",
        "authorization",
        "OPENAI_API_KEY",  # case-insensitive
    ],
)
def test_forbidden_key_name_rejected(forbidden_key: str) -> None:
    with pytest.raises(ScrubberViolationError, match="forbidden_key_name"):
        scrub_bundle_file("config.json", {forbidden_key: "value"})


@pytest.mark.unit
def test_string_over_content_limit_rejected() -> None:
    over = "a" * (CONTENT_STRING_LIMIT + 1)
    with pytest.raises(ScrubberViolationError, match="string_length_cap"):
        scrub_bundle_file("config.json", {"notes": over})


@pytest.mark.unit
def test_string_at_limit_passes() -> None:
    exactly = "a" * CONTENT_STRING_LIMIT
    scrub_bundle_file("config.json", {"notes": exactly})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "rule"),
    [
        # Runtime-constructed tokens so the static gitleaks scan on this
        # file never flags a test fixture as a real leak. The concatenation
        # form keeps the shape identical at runtime.
        ("AKIA" + "IOSFODNN7EXAMPLE", "aws_access_key"),
        ("sk-" + "1234567890abcdefghijklmn", "openai_api_key"),
        ("sk-ant-" + "abcdefghijklmnopqrstuvwxyz", "anthropic_api_key"),
        ("ghp_" + "A" * 36, "github_pat"),
        ("AIza" + "Sy" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ01234567", "google_api_key"),
        ("xoxb-" + "1111111111-2222222222-" + "ABCDEFGHIJ", "slack_token"),
        ("tessera_session_" + "A" * 24, "tessera_token"),
        ("-----BEGIN RSA PRIVATE KEY-----\nbody", "pem_private_key"),
    ],
)
def test_credential_regexes_fire(secret: str, rule: str) -> None:
    with pytest.raises(ScrubberViolationError, match=rule):
        scrub_bundle_file("x.json", {"x": secret})


@pytest.mark.unit
def test_nested_leak_is_caught() -> None:
    payload = {
        "recent_events": [
            {"attrs": {"inner": "ghp_" + "A" * 36}},
        ],
    }
    with pytest.raises(ScrubberViolationError, match="github_pat"):
        scrub_bundle_file("recent_events.jsonl", payload)


@pytest.mark.unit
def test_text_file_allows_long_schema_but_blocks_credentials() -> None:
    # A schema dump is legitimately long; text-file scrubber allows it.
    long_schema = "-- comment\n" + "CREATE TABLE x (c TEXT);\n" * 200
    scrub_text_file("schema.sql", long_schema)
    # But a credential inline is still rejected. Runtime concatenation
    # so gitleaks does not flag this source file on scan.
    leaked = "sk-" + "0123456789abcdefghijABCDEFGHIJKL"
    with pytest.raises(ScrubberViolationError, match="openai_api_key"):
        scrub_text_file("schema.sql", f"-- leaked\n-- {leaked}\n")


@pytest.mark.unit
def test_jsonl_file_walks_each_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"ok": true}\n{"token": "leak"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ScrubberViolationError, match="forbidden_key_name"):
        scrub_jsonl_file(path)


@pytest.mark.unit
def test_jsonl_file_handles_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ScrubberViolationError, match="could not parse"):
        scrub_jsonl_file(path)


@pytest.mark.unit
def test_non_string_scalars_never_leak() -> None:
    # Ints, floats, bools, None are by definition not secret-bearing.
    scrub_bundle_file("x", {"count": 42, "flag": True, "ratio": 0.5, "none": None})
