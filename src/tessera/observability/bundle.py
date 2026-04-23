"""``tessera doctor --collect <name>`` bundle builder.

Produces a single ``.tar.gz`` containing the files listed in
``docs/determinism-and-observability.md §Diagnostic bundles``. Every
file passes through :mod:`tessera.observability.scrub` before it
lands in the tarball; a scrubber violation aborts bundle creation
with a clear error so the tarball is never written in a leaky state.

The bundle is opt-in and user-initiated: the CLI instructs the user
to inspect the tarball before sharing and never auto-uploads. Per
``docs/non-goals.md`` this module has zero outbound-network
responsibilities and therefore zero outbound-network imports.
"""

from __future__ import annotations

import io
import json
import platform
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlcipher3

from tessera.observability.events import EventLog
from tessera.observability.scrub import (
    ScrubberViolationError,
    scrub_bundle_file,
    scrub_text_file,
)

# Cap recent_events.jsonl to a count that keeps bundle size sane but
# gives operators enough trail to reconstruct an incident. 500 events
# at ~256 bytes each = ~128 KiB uncompressed; gzip typically halves
# that. Audit counts are independently capped in audit_summary.jsonl.
DEFAULT_RECENT_EVENTS_LIMIT = 500
DEFAULT_RETRIEVAL_SAMPLES_LIMIT = 10
DEFAULT_AUDIT_SUMMARY_DAYS = 30


@dataclass(frozen=True, slots=True)
class BundleSpec:
    """Inputs the collector needs to produce a bundle."""

    vault_conn: sqlcipher3.Connection
    vault_path: Path
    event_log: EventLog | None
    tessera_version: str
    active_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BundleResult:
    """Where the bundle landed and what it contains.

    ``files`` is the list of in-tarball file names (not on-disk paths)
    so the CLI can print an explicit review list; the user opens the
    tarball and sees exactly what shipped.
    """

    tarball_path: Path
    files: tuple[str, ...]


def build_bundle(spec: BundleSpec, *, out_dir: Path, name: str) -> BundleResult:
    """Produce the tarball under ``out_dir`` with the given stem name.

    Raises :class:`ScrubberViolationError` on any leak-vector hit;
    the tarball is not written in that case. Callers surface the
    error to the user and abort the ``tessera doctor --collect``
    command with a nonzero exit.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = out_dir / f"{name}.tar.gz"

    env = _render_env(spec)
    scrub_bundle_file("env.json", env)
    config_dump = _render_config_dump(spec)
    scrub_bundle_file("config.json", config_dump)
    schema_text = _render_schema(spec.vault_conn)
    scrub_text_file("schema.sql", schema_text)
    stats = _render_stats(spec.vault_conn)
    scrub_bundle_file("stats.json", stats)
    recent_events = _render_recent_events(spec.event_log)
    for idx, event in enumerate(recent_events):
        scrub_bundle_file(f"recent_events.jsonl:{idx}", event)
    retrieval_samples = _render_retrieval_samples(spec.event_log)
    for idx, sample in enumerate(retrieval_samples):
        scrub_bundle_file(f"retrieval_samples.jsonl:{idx}", sample)
    audit_summary = _render_audit_summary(spec.vault_conn)
    for idx, row in enumerate(audit_summary):
        scrub_bundle_file(f"audit_summary.jsonl:{idx}", row)

    files = (
        "env.json",
        "config.json",
        "schema.sql",
        "stats.json",
        "recent_events.jsonl",
        "retrieval_samples.jsonl",
        "audit_summary.jsonl",
    )
    with tarfile.open(tarball_path, "w:gz") as tar:
        _add_json(tar, "env.json", env)
        _add_json(tar, "config.json", config_dump)
        _add_text(tar, "schema.sql", schema_text)
        _add_json(tar, "stats.json", stats)
        _add_jsonl(tar, "recent_events.jsonl", recent_events)
        _add_jsonl(tar, "retrieval_samples.jsonl", retrieval_samples)
        _add_jsonl(tar, "audit_summary.jsonl", audit_summary)
    return BundleResult(tarball_path=tarball_path, files=files)


# ---- file renderers ----------------------------------------------------


def _render_env(spec: BundleSpec) -> dict[str, Any]:
    return {
        "tessera_version": spec.tessera_version,
        "active_models": list(spec.active_models),
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "arch": platform.machine(),
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _render_config_dump(spec: BundleSpec) -> dict[str, Any]:
    """Return the subset of runtime config that is safe to ship.

    Paths, port numbers, and model names are fine. Passphrases,
    tokens, and keyring material are intentionally absent — the
    daemon config never carries them in-process under a form the
    bundle could accidentally serialise. The scrubber's key-name
    rules catch an accidental leak even if that ever changed.
    """

    return {
        "vault_path": str(spec.vault_path),
        "tessera_version": spec.tessera_version,
        "active_models": list(spec.active_models),
    }


def _render_schema(conn: sqlcipher3.Connection) -> str:
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
    ).fetchall()
    return "\n\n".join(str(r[0]).rstrip() + ";" for r in rows) + "\n"


def _render_stats(conn: sqlcipher3.Connection) -> dict[str, Any]:
    facet_count = int(
        conn.execute("SELECT COUNT(*) FROM facets WHERE is_deleted = 0").fetchone()[0]
    )
    by_type = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT facet_type, COUNT(*)
            FROM facets
            WHERE is_deleted = 0
            GROUP BY facet_type
            ORDER BY facet_type
            """
        ).fetchall()
    }
    embed_status = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT embed_status, COUNT(*)
            FROM facets
            WHERE is_deleted = 0
            GROUP BY embed_status
            """
        ).fetchall()
    }
    return {
        "facet_count": facet_count,
        "by_type": by_type,
        "embed_status": embed_status,
    }


def _render_recent_events(event_log: EventLog | None) -> list[dict[str, Any]]:
    if event_log is None:
        return []
    rows = event_log.recent(limit=DEFAULT_RECENT_EVENTS_LIMIT, min_level="info")
    return [
        {
            "at": r.at,
            "level": r.level,
            "category": r.category,
            "event": r.event,
            "attrs": _safe_attrs(r.attrs),
            "duration_ms": r.duration_ms,
            "correlation_id": r.correlation_id,
        }
        for r in rows
    ]


def _render_retrieval_samples(event_log: EventLog | None) -> list[dict[str, Any]]:
    if event_log is None:
        return []
    rows = event_log.recent_by_event(event="recall_slow", limit=DEFAULT_RETRIEVAL_SAMPLES_LIMIT)
    return [
        {
            "at": r.at,
            "duration_ms": r.duration_ms,
            "attrs": _safe_attrs(r.attrs),
        }
        for r in rows
    ]


def _render_audit_summary(conn: sqlcipher3.Connection) -> list[dict[str, Any]]:
    """Counts per audit op per day for the last N days.

    The audit table carries op names and ULIDs only (no content); the
    counts are even safer — they are aggregates. Grouping per-day
    keeps the file bounded by ``ops * days`` rather than row count.
    """

    cutoff = int(datetime.now(UTC).timestamp()) - DEFAULT_AUDIT_SUMMARY_DAYS * 24 * 60 * 60
    rows = conn.execute(
        """
        SELECT op,
               date(at, 'unixepoch') AS day,
               COUNT(*)
        FROM audit_log
        WHERE at >= ?
        GROUP BY op, day
        ORDER BY day DESC, op
        """,
        (cutoff,),
    ).fetchall()
    return [{"day": str(row[1]), "op": str(row[0]), "count": int(row[2])} for row in rows]


def _safe_attrs(attrs: Mapping[str, Any]) -> dict[str, Any]:
    # Events.db attrs are already bounded to 4 KiB and emitted by
    # vetted call sites, but the scrubber walks them anyway when we
    # scrub_bundle_file the event row. No re-sanitisation needed.
    return dict(attrs)


# ---- tar writers -------------------------------------------------------


def _add_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(info, io.BytesIO(data))


def _add_json(tar: tarfile.TarFile, name: str, payload: Any) -> None:
    _add_text(tar, name, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _add_jsonl(tar: tarfile.TarFile, name: str, rows: list[dict[str, Any]]) -> None:
    body = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    if body:
        body += "\n"
    _add_text(tar, name, body)


def review_instructions(bundle: BundleResult) -> str:
    """Plain-text block the CLI prints next to the bundle path.

    The operator reads this every time they run the command, so the
    wording stays short and imperative: open the archive, inspect
    each file, decide whether to share. No automation, no upload.
    """

    return (
        f"Bundle written to {bundle.tarball_path}.\n"
        "Before sharing, open the archive and review each file:\n"
        + "\n".join(f"  - {f}" for f in bundle.files)
        + "\nTessera does not upload bundles automatically; "
        "only you decide what, if anything, to attach to a bug report.\n"
    )


__all__ = [
    "DEFAULT_AUDIT_SUMMARY_DAYS",
    "DEFAULT_RECENT_EVENTS_LIMIT",
    "DEFAULT_RETRIEVAL_SAMPLES_LIMIT",
    "BundleResult",
    "BundleSpec",
    "ScrubberViolationError",
    "build_bundle",
    "review_instructions",
]
