"""Append-only JSONL ledger for dogfood evidence.

One file per gate (``sync.jsonl``, ``compiled.jsonl``, ``playbook.jsonl``)
under ``$TESSERA_HOME/dogfood/`` (default ``~/.tessera/dogfood/``).
Each line is one :class:`tessera.dogfood.schemas.Record`.

Concurrency: appends take an ``fcntl.flock`` exclusive lock on the
ledger file so two parallel ``tessera`` invocations cannot interleave
partial writes. Reads walk the file under a shared lock.

Auto-emission policy: ``auto_record`` is the side-channel that
existing CLI commands call after they finish their primary work. It
fails loud (``DogfoodEmissionError``) so the call site can decide to
``warn``-but-not-fail; auto-emission must never break the user's
actual command. ``TESSERA_DOGFOOD_DISABLE=1`` skips emission entirely
for users who do not want any sidecar at all.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from tessera.dogfood.schemas import (
    GATE_KINDS,
    GATES,
    Record,
    SchemaError,
)

DEFAULT_LEDGER_DIR: Final[Path] = Path("~/.tessera/dogfood").expanduser()
_DISABLE_ENV_VAR: Final[str] = "TESSERA_DOGFOOD_DISABLE"
_DIR_ENV_VAR: Final[str] = "TESSERA_DOGFOOD_DIR"


class DogfoodEmissionError(RuntimeError):
    """Raised when an auto-emission attempt fails after a record was built.

    Call sites surface this as a warning (``warn``) without failing
    the user's actual command — auto-emission is instrumentation and
    must not gate the primary path.
    """


class LedgerCorruptionError(RuntimeError):
    """Raised when reading a ledger file finds a malformed line.

    Carries the 1-indexed line number so the operator can repair the
    bad row in place; the rest of the file is not silently dropped.
    """

    def __init__(self, *, path: Path, line_number: int, reason: str) -> None:
        super().__init__(f"ledger {path} line {line_number}: {reason}")
        self.path = path
        self.line_number = line_number
        self.reason = reason


def is_disabled() -> bool:
    """True when ``TESSERA_DOGFOOD_DISABLE=1`` is set in the environment."""

    return os.environ.get(_DISABLE_ENV_VAR) == "1"


def ledger_dir(*, base_dir: Path | None = None) -> Path:
    """Return the dogfood ledger directory, honoring the env override.

    Resolution order: explicit ``base_dir`` arg → ``$TESSERA_DOGFOOD_DIR``
    → ``~/.tessera/dogfood/``. The directory is not created here; the
    Ledger creates it lazily on first append.
    """

    if base_dir is not None:
        return base_dir.expanduser()
    env = os.environ.get(_DIR_ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_LEDGER_DIR


def ledger_path(gate: str, *, base_dir: Path | None = None) -> Path:
    """Return the JSONL path for ``gate`` under the ledger directory."""

    if gate not in GATES:
        raise SchemaError(f"unknown gate: {gate!r}")
    return ledger_dir(base_dir=base_dir) / f"{gate}.jsonl"


@dataclass(frozen=True, slots=True)
class GateState:
    """Lifecycle snapshot for a single gate's ledger.

    ``initialized`` — at least one ``gate_initialized`` row exists.
    ``completed`` — the most recent lifecycle row is ``gate_completed``.
    ``active`` — initialized and not completed (auto-emission target).
    ``rows`` — total row count (informational).
    """

    initialized: bool
    completed: bool
    active: bool
    rows: int


class Ledger:
    """One gate's JSONL ledger.

    The Ledger does not cache rows in memory: every read walks the
    file. Append uses exclusive flock; iter uses shared flock. The
    file is created on first append (``mkdir -p`` on the parent).
    """

    def __init__(self, gate: str, *, base_dir: Path | None = None) -> None:
        if gate not in GATES:
            raise SchemaError(f"unknown gate: {gate!r}")
        self.gate = gate
        self.path = ledger_path(gate, base_dir=base_dir)

    def append(self, record: Record) -> None:
        """Append one record under an exclusive flock.

        Validates that ``record.gate`` matches this ledger; mismatches
        are a wiring bug, not a schema bug, and surface as
        :class:`SchemaError` rather than silently writing to the wrong
        file.
        """

        if record.gate != self.gate:
            raise SchemaError(
                f"record.gate={record.gate!r} does not match ledger gate {self.gate!r}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = record.to_jsonl_line() + "\n"
        with self.path.open("a", encoding="utf-8") as fh, _exclusive_flock(fh):
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def iter_records(self) -> Iterator[Record]:
        """Yield every record from the file, in stored order.

        Raises :class:`LedgerCorruptionError` on the first malformed
        line; the caller decides whether to surface or repair. We do
        not silently drop rows because the gate evidence depends on
        every row being trustworthy.
        """

        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh, _shared_flock(fh):
            for line_number, line in enumerate(fh, start=1):
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                try:
                    record = Record.from_jsonl_line(stripped)
                except SchemaError as exc:
                    raise LedgerCorruptionError(
                        path=self.path,
                        line_number=line_number,
                        reason=str(exc),
                    ) from exc
                if record.gate != self.gate:
                    raise LedgerCorruptionError(
                        path=self.path,
                        line_number=line_number,
                        reason=f"row gate {record.gate!r} != ledger gate {self.gate!r}",
                    )
                yield record

    def state(self) -> GateState:
        """Compute lifecycle state by walking the ledger.

        ``active`` is the load-bearing flag for auto-emission: a gate
        is active when its most recent lifecycle row (``gate_initialized``
        or ``gate_completed``) is ``gate_initialized``. A ledger with
        zero rows is not active; a ledger that has been initialized and
        then completed is not active.
        """

        rows = 0
        last_lifecycle: str | None = None
        initialized = False
        for record in self.iter_records():
            rows += 1
            if record.kind == "gate_initialized":
                initialized = True
                last_lifecycle = "gate_initialized"
            elif record.kind == "gate_completed":
                last_lifecycle = "gate_completed"
        completed = last_lifecycle == "gate_completed"
        active = initialized and not completed
        return GateState(
            initialized=initialized,
            completed=completed,
            active=active,
            rows=rows,
        )

    def latest(self, *, kind: str | None = None) -> Record | None:
        """Return the most recent record, optionally filtered by kind.

        Walks the file once; constant memory.
        """

        latest: Record | None = None
        for record in self.iter_records():
            if kind is None or record.kind == kind:
                latest = record
        return latest


def auto_record(
    *,
    kind: str,
    payload: dict[str, Any],
    gates: list[str] | None = None,
    base_dir: Path | None = None,
) -> list[str]:
    """Best-effort append to every active gate that admits ``kind``.

    Returns the list of gate names that received the row. An empty
    list means no gate was active or no gate admits ``kind`` — both
    are normal (the operator may be running ``audit verify`` without
    any dogfood gate in flight).

    Raises :class:`DogfoodEmissionError` when a gate is active and
    admits the kind but the append itself fails (filesystem error,
    schema drift). The caller surfaces this as a warning so the
    primary command's exit code reflects only the primary action.
    """

    if is_disabled():
        return []
    targets = list(GATES) if gates is None else gates
    recorded: list[str] = []
    failures: list[str] = []
    for gate in targets:
        if kind not in GATE_KINDS.get(gate, frozenset()):
            continue
        ledger = Ledger(gate, base_dir=base_dir)
        try:
            state = ledger.state()
        except LedgerCorruptionError as exc:
            failures.append(f"{gate}: read failed: {exc}")
            continue
        if not state.active:
            continue
        try:
            record = Record.make(gate=gate, kind=kind, payload=payload)
            ledger.append(record)
        except (SchemaError, OSError) as exc:
            failures.append(f"{gate}: append failed: {exc}")
            continue
        recorded.append(gate)
    if failures:
        raise DogfoodEmissionError("; ".join(failures))
    return recorded


@contextlib.contextmanager
def _exclusive_flock(fh: IO[str]) -> Iterator[None]:
    """Take an exclusive flock for the duration of the block."""

    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _shared_flock(fh: IO[str]) -> Iterator[None]:
    """Take a shared flock for the duration of the block."""

    fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


__all__ = [
    "DEFAULT_LEDGER_DIR",
    "DogfoodEmissionError",
    "GateState",
    "Ledger",
    "LedgerCorruptionError",
    "auto_record",
    "is_disabled",
    "ledger_dir",
    "ledger_path",
]
