"""Atomic config-file writes with a pre-write backup.

Client config files (``claude_desktop_config.json``, ``~/.cursor/mcp.json``,
``~/.codex/config.toml``, ...) are user-authored and may carry unrelated
keys the user cares about. A connect/disconnect operation must never
corrupt them. This module is the primitive every per-client writer
sits on top of.

The guarantees:

1. **Pre-write backup.** Before the first byte of the new config is
   written, a byte-identical copy of the existing file lands at
   ``<path>.tessera-backup-<timestamp>``. The connector returns the
   backup path so the caller can surface it.
2. **Atomic replace.** The new bytes land in a temp file in the same
   directory; the temp file is ``os.replace``-ed over the target. On
   Unix this is atomic — a reader of ``<path>`` sees either the old
   state or the new state, never a half-written file.
3. **Shape-preserving merge.** The writer does not rewrite the whole
   file; it merges a delta into the parsed structure and serialises
   back. Keys outside the merge target are preserved byte-for-byte
   (to the extent the parser-round-trip allows — JSON preserves all
   keys and types; TOML may reorder comments, which is called out in
   the docstring below).
4. **Unknown-shape fail-loud.** If the parsed structure does not match
   the caller's expected shape (e.g. a JSON array where a dict was
   expected), the writer raises :class:`UnsupportedConfigShapeError`
   rather than coercing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w


class FileSafetyError(Exception):
    """Base class for config-file-safety failures."""


class UnsupportedConfigShapeError(FileSafetyError):
    """Parsed config does not match the expected top-level shape."""


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Outcome of a safe write.

    ``backup_path`` is ``None`` when the target did not exist before
    the write (nothing to back up). ``already_matches`` is True when
    the merge produced identical bytes — the writer still records the
    non-change so connectors can report a stable "no-op" signal
    instead of mistaking it for a silent failure.
    """

    path: Path
    backup_path: Path | None
    already_matches: bool


def read_json(path: Path) -> dict[str, Any]:
    """Load ``path`` as JSON; return ``{}`` if the file does not exist.

    Raises :class:`UnsupportedConfigShapeError` when the file exists
    but its top-level value is not a JSON object. Callers rely on this
    to fail loudly rather than silently coerce, e.g. a JSON array in a
    config file that should carry an object is a user mistake worth
    surfacing.
    """

    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UnsupportedConfigShapeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise UnsupportedConfigShapeError(
            f"{path} top-level must be a JSON object, got {type(loaded).__name__}"
        )
    return loaded


def read_toml(path: Path) -> dict[str, Any]:
    """Load ``path`` as TOML; return ``{}`` if the file does not exist.

    TOML's data model is strictly a dict at the top level (unlike JSON,
    which could be a scalar or array at the root). The ``isinstance``
    check stays for defensive parity with :func:`read_json`, but TOML
    parsers never surface anything other than a dict from a valid
    document.
    """

    if not path.exists():
        return {}
    try:
        loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise UnsupportedConfigShapeError(f"{path} is not valid TOML: {exc}") from exc
    if not isinstance(loaded, dict):  # pragma: no cover — tomllib invariant
        raise UnsupportedConfigShapeError(
            f"{path} top-level must be a TOML table, got {type(loaded).__name__}"
        )
    return loaded


def write_safely(
    path: Path,
    payload: dict[str, Any],
    *,
    serialiser: Callable[[dict[str, Any]], bytes],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WriteOutcome:
    """Write ``payload`` to ``path`` atomically with a pre-write backup.

    Creates parent directories on demand. When ``path`` already exists,
    copies it to ``<path>.tessera-backup-<UTC timestamp>`` before the
    atomic replace. When the serialised payload is byte-identical to
    the existing file, no backup is taken and
    ``already_matches=True`` is returned.
    """

    new_bytes = serialiser(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing == new_bytes:
            return WriteOutcome(path=path, backup_path=None, already_matches=True)
        backup_path = path.with_name(
            f"{path.name}.tessera-backup-{now().strftime('%Y%m%dT%H%M%SZ')}"
        )
        shutil.copy2(path, backup_path)
    else:
        backup_path = None
    # Same-directory temp file so ``os.replace`` is atomic on every
    # POSIX filesystem. A context-managed NamedTemporaryFile guarantees
    # the file handle is released before the rename even if
    # ``os.replace`` raises, which matters on Windows where the target
    # must not be open.
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.tessera-tmp-",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(new_bytes)
        tmp.flush()
    try:
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return WriteOutcome(path=path, backup_path=backup_path, already_matches=False)


def json_serialiser(payload: dict[str, Any]) -> bytes:
    """Serialise a JSON payload with stable two-space indent + trailing newline.

    Claude Desktop / Claude Code / Cursor configs are edited by humans;
    the two-space indent + trailing newline matches how those apps
    write the file themselves, keeping diffs minimal when the user
    inspects the file after ``tessera connect``.
    """

    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def toml_serialiser(payload: dict[str, Any]) -> bytes:
    """Serialise a TOML payload.

    tomli-w is the canonical writer paired with the stdlib tomllib
    reader. Comments and formatting whitespace are not preserved on
    round-trip — this is a known limitation of the TOML data model
    plus tomli-w. Connectors that touch Codex's config.toml call it
    out in their docstring.
    """

    return tomli_w.dumps(payload).encode("utf-8")


__all__ = [
    "FileSafetyError",
    "UnsupportedConfigShapeError",
    "WriteOutcome",
    "json_serialiser",
    "read_json",
    "read_toml",
    "toml_serialiser",
    "write_safely",
]
