"""events.db — structured event log for operational debugging.

Per ``docs/determinism-and-observability.md §Structured event log``
the daemon emits slow-query, embed-pipeline, and capability-lifecycle
events to a separate ``~/.tessera/events.db`` SQLite file. The log is
**not** the audit log: audit captures legal-grade mutations, events
capture operational telemetry. Keeping them in distinct files lets an
operator wipe the events database without touching forensic records.

Events are local-only. Nothing in this module speaks to the network.
The file is a plain SQLite DB (not sqlcipher) so a user or operator
can inspect it with ``sqlite3`` without knowing the vault passphrase;
there is no facet content here to protect.

Rolling retention: events older than
:data:`DEFAULT_RETENTION_SECONDS` are swept on every call to
:func:`sweep`. The daemon calls ``sweep`` once a day on a background
cadence; ``sweep`` is idempotent and cheap on an empty DB.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

EventLevel = Literal["debug", "info", "warn", "error"]

# 7 days by default per docs/determinism-and-observability.md §Retention.
# Operators who want a tighter or looser window configure it via the
# caller; the default only kicks in when the caller does not override.
DEFAULT_RETENTION_SECONDS: Final[int] = 7 * 24 * 60 * 60

# Events must stay small so ``events.db`` never balloons. A 4 KiB cap
# on the serialised ``attrs`` JSON is three orders of magnitude above
# the per-event payload this module emits; anything larger is a bug
# on the emitter side and the cap surfaces it as a validation error.
_MAX_ATTRS_BYTES: Final[int] = 4 * 1024


_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS events (
        id              INTEGER PRIMARY KEY,
        at              INTEGER NOT NULL,
        level           TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warn', 'error')),
        category        TEXT NOT NULL,
        event           TEXT NOT NULL,
        attrs           TEXT NOT NULL DEFAULT '{}',
        duration_ms     INTEGER,
        correlation_id  TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS events_at
        ON events(at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS events_cat
        ON events(category, level, at DESC)
    """,
)


class EventLogError(Exception):
    """Base class for event-log failures."""


class OversizedAttrsError(EventLogError):
    """Emitter tried to write an attrs blob above the per-event cap."""


@dataclass(frozen=True, slots=True)
class Event:
    """One row in ``events.db``."""

    id: int
    at: int
    level: EventLevel
    category: str
    event: str
    attrs: dict[str, Any]
    duration_ms: int | None
    correlation_id: str | None


@dataclass
class EventLog:
    """Thin wrapper around a plain sqlite3 connection at ``path``.

    Opening an EventLog is idempotent: the DDL runs under
    ``CREATE ... IF NOT EXISTS``, so multiple processes pointing at
    the same file converge without coordination. The underlying
    connection is kept for the lifetime of the EventLog so callers
    can reuse it across many ``emit`` calls without paying repeated
    open costs.
    """

    path: Path
    _conn: sqlite3.Connection

    @classmethod
    def open(cls, path: Path) -> EventLog:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        for stmt in _DDL:
            conn.execute(stmt)
        return cls(path=path, _conn=conn)

    def close(self) -> None:
        self._conn.close()

    def emit(
        self,
        *,
        level: EventLevel,
        category: str,
        event: str,
        attrs: Mapping[str, Any] | None = None,
        duration_ms: int | None = None,
        correlation_id: str | None = None,
        at: int | None = None,
    ) -> int:
        """Append one event; return the inserted rowid.

        Raises :class:`OversizedAttrsError` when the serialised
        ``attrs`` exceeds :data:`_MAX_ATTRS_BYTES`. The ceiling is the
        last line of defense against accidental content leakage into
        the event log — every documented emitter stays well under it.
        """

        payload = dict(attrs or {})
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > _MAX_ATTRS_BYTES:
            raise OversizedAttrsError(
                f"attrs for {category}/{event} exceeds {_MAX_ATTRS_BYTES} bytes"
            )
        cur = self._conn.execute(
            """
            INSERT INTO events(at, level, category, event, attrs, duration_ms, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                at if at is not None else _now_epoch(),
                level,
                category,
                event,
                encoded,
                duration_ms,
                correlation_id,
            ),
        )
        if cur.lastrowid is None:  # pragma: no cover — sqlite invariant
            raise EventLogError("events INSERT produced no rowid")
        return int(cur.lastrowid)

    def recent(
        self,
        *,
        limit: int,
        min_level: EventLevel = "info",
    ) -> list[Event]:
        """Return the most recent ``limit`` events at or above ``min_level``.

        ``debug`` events are excluded from diagnostic bundles by
        default per the bundle spec; callers that want them pass
        ``min_level='debug'``.
        """

        allowed = _levels_at_or_above(min_level)
        placeholders = ",".join("?" for _ in allowed)
        rows = self._conn.execute(
            f"""
            SELECT id, at, level, category, event, attrs, duration_ms, correlation_id
            FROM events
            WHERE level IN ({placeholders})
            ORDER BY at DESC, id DESC
            LIMIT ?
            """,
            (*allowed, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def recent_by_event(self, *, event: str, limit: int) -> list[Event]:
        """Return the most recent ``limit`` rows whose ``event`` matches."""

        rows = self._conn.execute(
            """
            SELECT id, at, level, category, event, attrs, duration_ms, correlation_id
            FROM events
            WHERE event = ?
            ORDER BY at DESC, id DESC
            LIMIT ?
            """,
            (event, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def sweep(
        self,
        *,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        now_epoch: int | None = None,
    ) -> int:
        """Drop events older than ``retention_seconds``; return the count removed."""

        cutoff = (now_epoch if now_epoch is not None else _now_epoch()) - retention_seconds
        cur = self._conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        return int(cur.rowcount)

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])


@contextmanager
def open_event_log(path: Path) -> Iterator[EventLog]:
    log = EventLog.open(path)
    try:
        yield log
    finally:
        log.close()


def _levels_at_or_above(min_level: EventLevel) -> list[str]:
    order: list[EventLevel] = ["debug", "info", "warn", "error"]
    idx = order.index(min_level)
    return list(order[idx:])


def _row_to_event(row: tuple[Any, ...]) -> Event:
    return Event(
        id=int(row[0]),
        at=int(row[1]),
        level=row[2],
        category=str(row[3]),
        event=str(row[4]),
        attrs=json.loads(row[5]) if row[5] else {},
        duration_ms=int(row[6]) if row[6] is not None else None,
        correlation_id=str(row[7]) if row[7] is not None else None,
    )


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


__all__ = [
    "DEFAULT_RETENTION_SECONDS",
    "Event",
    "EventLevel",
    "EventLog",
    "EventLogError",
    "OversizedAttrsError",
    "open_event_log",
]
