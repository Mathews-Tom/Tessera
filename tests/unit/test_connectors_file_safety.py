"""Atomic-write + pre-write-backup invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera.connectors.file_safety import (
    UnsupportedConfigShapeError,
    json_serialiser,
    read_json,
    read_toml,
    toml_serialiser,
    write_safely,
)


def _fixed_now() -> datetime:
    return datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_read_json_returns_empty_dict_when_missing(tmp_path: Path) -> None:
    assert read_json(tmp_path / "missing.json") == {}


@pytest.mark.unit
def test_read_json_rejects_array_top_level(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(UnsupportedConfigShapeError, match="JSON object"):
        read_json(path)


@pytest.mark.unit
def test_read_json_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("not json {")
    with pytest.raises(UnsupportedConfigShapeError, match="not valid JSON"):
        read_json(path)


@pytest.mark.unit
def test_read_toml_returns_empty_dict_when_missing(tmp_path: Path) -> None:
    assert read_toml(tmp_path / "missing.toml") == {}


@pytest.mark.unit
def test_read_toml_rejects_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("not = toml = here")
    with pytest.raises(UnsupportedConfigShapeError, match="not valid TOML"):
        read_toml(path)


@pytest.mark.unit
def test_write_safely_creates_file_without_backup(tmp_path: Path) -> None:
    path = tmp_path / "new.json"
    outcome = write_safely(
        path,
        {"a": 1},
        serialiser=json_serialiser,
        now=_fixed_now,
    )
    assert outcome.path == path
    assert outcome.backup_path is None
    assert outcome.already_matches is False
    assert path.read_text().endswith("\n")
    assert read_json(path) == {"a": 1}


@pytest.mark.unit
def test_write_safely_backs_up_when_content_changes(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text('{"a": 1}\n')
    outcome = write_safely(
        path,
        {"a": 2},
        serialiser=json_serialiser,
        now=_fixed_now,
    )
    assert outcome.backup_path is not None
    assert outcome.backup_path.exists()
    assert outcome.backup_path.read_text() == '{"a": 1}\n'
    assert read_json(path) == {"a": 2}


@pytest.mark.unit
def test_write_safely_noop_when_payload_matches(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_bytes(json_serialiser({"a": 1}))
    outcome = write_safely(
        path,
        {"a": 1},
        serialiser=json_serialiser,
        now=_fixed_now,
    )
    assert outcome.already_matches is True
    assert outcome.backup_path is None
    # No tessera-backup file got written for the no-op.
    assert not list(path.parent.glob("cfg.json.tessera-backup-*"))


@pytest.mark.unit
def test_write_safely_toml_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cfg.toml"
    write_safely(
        path,
        {"mcp_servers": {"tessera": {"url": "http://127.0.0.1:5710/mcp"}}},
        serialiser=toml_serialiser,
    )
    loaded = read_toml(path)
    assert loaded == {"mcp_servers": {"tessera": {"url": "http://127.0.0.1:5710/mcp"}}}


@pytest.mark.unit
def test_write_safely_preserves_sibling_keys_through_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text('{"user": "tom", "a": 1}\n')
    outcome = write_safely(
        path,
        {"user": "tom", "a": 2, "new": "key"},
        serialiser=json_serialiser,
        now=_fixed_now,
    )
    assert outcome.backup_path is not None
    assert read_json(path) == {"user": "tom", "a": 2, "new": "key"}
