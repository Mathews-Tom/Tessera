"""events.db schema, emit semantics, retention sweep."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.observability.events import (
    DEFAULT_RETENTION_SECONDS,
    EventLog,
    OversizedAttrsError,
)


@pytest.mark.unit
def test_open_creates_schema_on_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    log = EventLog.open(path)
    try:
        assert path.exists()
        assert log.count() == 0
    finally:
        log.close()


@pytest.mark.unit
def test_emit_round_trip(tmp_path: Path) -> None:
    log = EventLog.open(tmp_path / "events.db")
    try:
        rowid = log.emit(
            level="info",
            category="embed",
            event="embed_succeeded",
            attrs={"facet_id": 42, "model_id": 1},
            at=1_700_000_000,
        )
        assert rowid == 1
        rows = log.recent(limit=10, min_level="info")
        assert len(rows) == 1
        assert rows[0].event == "embed_succeeded"
        assert rows[0].attrs == {"facet_id": 42, "model_id": 1}
        assert rows[0].at == 1_700_000_000
    finally:
        log.close()


@pytest.mark.unit
def test_recent_filters_by_min_level(tmp_path: Path) -> None:
    log = EventLog.open(tmp_path / "events.db")
    try:
        log.emit(level="debug", category="x", event="noise", at=1)
        log.emit(level="info", category="x", event="ok", at=2)
        log.emit(level="warn", category="x", event="yellow", at=3)
        log.emit(level="error", category="x", event="red", at=4)
        events = [e.event for e in log.recent(limit=10, min_level="warn")]
        assert events == ["red", "yellow"]
    finally:
        log.close()


@pytest.mark.unit
def test_oversized_attrs_rejected(tmp_path: Path) -> None:
    log = EventLog.open(tmp_path / "events.db")
    try:
        with pytest.raises(OversizedAttrsError):
            log.emit(level="info", category="x", event="big", attrs={"blob": "x" * 5_000})
    finally:
        log.close()


@pytest.mark.unit
def test_sweep_drops_stale_entries(tmp_path: Path) -> None:
    log = EventLog.open(tmp_path / "events.db")
    try:
        log.emit(level="info", category="x", event="old", at=0)
        log.emit(level="info", category="x", event="new", at=1_000_000)
        removed = log.sweep(now_epoch=1_000_000)
        assert removed == 1
        assert log.count() == 1
    finally:
        log.close()


@pytest.mark.unit
def test_default_retention_matches_spec() -> None:
    # docs/determinism-and-observability.md §Retention: 7 days.
    assert DEFAULT_RETENTION_SECONDS == 7 * 24 * 60 * 60


@pytest.mark.unit
def test_recent_by_event_is_filtered(tmp_path: Path) -> None:
    log = EventLog.open(tmp_path / "events.db")
    try:
        log.emit(level="info", category="retrieval", event="recall_slow", at=1)
        log.emit(level="info", category="retrieval", event="recall_slow", at=2)
        log.emit(level="info", category="embed", event="embed_succeeded", at=3)
        slow = log.recent_by_event(event="recall_slow", limit=10)
        assert len(slow) == 2
        assert all(r.event == "recall_slow" for r in slow)
    finally:
        log.close()
