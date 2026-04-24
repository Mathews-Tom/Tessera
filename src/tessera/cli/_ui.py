"""Shared Rich-powered terminal UI for every ``tessera`` subcommand.

Design contract:
- **TTY-aware.** The :data:`console` singleton auto-detects whether stdout
  is a terminal via ``rich.console.Console.is_terminal``. When Tessera's
  output is piped (``tessera agents create | tee log``), Rich emits
  plain text without ANSI escapes, so downstream parsers keep working.
  This module adds nothing that defeats that — emojis render when the
  terminal supports them, otherwise Rich falls back silently.
- **Error channel is stderr.** :data:`err_console` writes to stderr so
  diagnostics do not contaminate stdout pipelines. Error helpers
  (:func:`error`, :func:`fail`) go through the error console.
- **Status tokens are stable.** The literal words ``OK``, ``WARN``, and
  ``ERROR`` appear in every status line regardless of TTY mode so
  ``tessera doctor | grep ERROR`` still works.
- **No fallback hacks.** Every helper is typed; non-TTY behaviour is
  Rich's built-in, not a conditional branch we maintain.

Callers never construct a :class:`rich.console.Console` directly — the
module-level :data:`console` and :data:`err_console` are the sole
instances so theme, TTY detection, and no-color behaviour stay uniform.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# Theme keys used by every subcommand so colour choices stay consistent.
_THEME = Theme(
    {
        "tessera.success": "bold green",
        "tessera.warn": "bold yellow",
        "tessera.error": "bold red",
        "tessera.info": "bold cyan",
        "tessera.vault": "bold blue",
        "tessera.facet": "magenta",
        "tessera.dim": "dim",
        "tessera.kbd": "bold white on grey23",
    }
)

# Respect ``NO_COLOR`` per https://no-color.org — if set, Rich skips
# ANSI escapes even on a TTY. ``TESSERA_NO_COLOR`` is a narrower
# override for users who want colour elsewhere but not inside Tessera.
_NO_COLOR = bool(os.environ.get("NO_COLOR") or os.environ.get("TESSERA_NO_COLOR"))

console: Final[Console] = Console(theme=_THEME, no_color=_NO_COLOR, soft_wrap=False)
err_console: Final[Console] = Console(
    theme=_THEME, no_color=_NO_COLOR, stderr=True, soft_wrap=False
)

# Emoji tokens, used consistently across commands. Terminal support is
# broad on macOS / modern Linux; Rich strips them when the terminal
# reports no emoji capability.
EMOJI: Final[dict[str, str]] = {
    "ok": "✓",
    "warn": "⚠",
    "error": "✗",
    "info": "ℹ",  # noqa: RUF001 — INFORMATION SOURCE is intentional
    "vault": "🔐",
    "daemon_up": "🚀",
    "daemon_down": "🛑",
    "capture": "📝",
    "recall": "🧠",
    "doctor": "🔍",
    "export": "📦",
    "import": "📥",
    "connect": "🔌",
    "models": "🧰",
    "repair": "♻",
    "token": "🎟",
    "agent": "🙂",
    "forget": "🧹",
}


def success(message: str, *, emoji: str = EMOJI["ok"]) -> None:
    """Print a green success line to stdout, prefixed with ✓.

    The literal word ``OK`` is not injected here because success lines
    are usually declarative sentences; the green ✓ is the status signal.
    Callers who need grep-stable tokens should include them in ``message``.
    """

    console.print(f"[tessera.success]{emoji}[/] {message}")


def warn(message: str, *, emoji: str = EMOJI["warn"]) -> None:
    """Print a yellow WARN line to stdout with the literal token ``WARN``."""

    console.print(f"[tessera.warn]{emoji} WARN[/] {message}")


def error(message: str, *, emoji: str = EMOJI["error"]) -> None:
    """Print a red ERROR line to stderr with the literal token ``ERROR``."""

    err_console.print(f"[tessera.error]{emoji} ERROR[/] {message}")


def info(message: str, *, emoji: str = EMOJI["info"]) -> None:
    """Print a cyan info line to stdout."""

    console.print(f"[tessera.info]{emoji}[/] {message}")


def fail(message: str, *, emoji: str = EMOJI["error"], code: int = 1) -> int:
    """Print an ERROR line to stderr and return the exit code.

    Replaces the bare :func:`tessera.cli._common.fail` when the caller
    can import the UI module. Returning an ``int`` matches the CLI
    handler signature so ``return fail(...)`` is the idiomatic call site.
    """

    error(message, emoji=emoji)
    return code


def raw(line: str) -> None:
    """Emit a line to stdout without any formatting at all.

    For machine-parseable outputs (agent IDs, capability tokens, file
    paths) the caller should use :func:`raw` rather than :func:`success`
    so that piping the output to another tool gives exactly the bare
    value. On a TTY the line still renders plain; on a pipe it reaches
    the downstream reader unchanged.
    """

    console.print(line, markup=False, highlight=False)


@contextmanager
def status(message: str, *, emoji: str = EMOJI["info"]) -> Iterator[None]:
    """Show an infinite spinner while the wrapped block runs.

    Rich's :meth:`rich.console.Console.status` auto-chooses a spinner
    style per TTY capability; the fallback on dumb terminals is a
    single static line. Non-TTY output drops the spinner entirely —
    downstream pipelines see nothing until the block prints its own
    result. That matches the "machine-readable pipe" contract above.
    """

    with console.status(f"[tessera.info]{emoji}[/] {message}", spinner="dots"):
        yield


@contextmanager
def progress(description: str, *, total: int | None = None) -> Iterator[Any]:
    """Start a progress bar with a spinner, elapsed time, and an optional total.

    Yields the :class:`rich.progress.Progress` instance and a
    pre-started task id. Callers advance the task via
    ``prog.update(task, advance=1)`` inside their loop.

    Use :func:`status` for work without a countable total; use
    :func:`progress` for iteration over a known collection (e.g. facets
    to re-embed, rows to import).
    """

    prog = Progress(
        SpinnerColumn(style="tessera.info"),
        TextColumn("[tessera.info]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with prog:
        task_id = prog.add_task(description, total=total)
        yield _ProgressHandle(prog, task_id)


class _ProgressHandle:
    """Friendly wrapper so callers don't juggle raw Progress + task_id."""

    __slots__ = ("_prog", "_task_id")

    def __init__(self, prog: Progress, task_id: TaskID) -> None:
        self._prog = prog
        self._task_id = task_id

    def advance(self, steps: int = 1) -> None:
        self._prog.update(self._task_id, advance=steps)

    def update(self, *, description: str | None = None, total: int | None = None) -> None:
        if description is not None:
            self._prog.update(self._task_id, description=description)
        if total is not None:
            self._prog.update(self._task_id, total=total)


def kv_panel(title: str, items: dict[str, str], *, emoji: str | None = None) -> None:
    """Render a key-value block inside a bordered panel on TTY.

    Degrades to a plain ``key: value`` list on pipe, because Rich
    removes the border drawing when no TTY is detected.
    """

    body = Text()
    for i, (key, value) in enumerate(items.items()):
        if i > 0:
            body.append("\n")
        body.append(f"{key}: ", style="tessera.dim")
        body.append(value)
    header = f"{emoji} {title}" if emoji else title
    console.print(Panel(body, title=header, border_style="tessera.info", expand=False))


def report_table(title: str, columns: list[str], *, emoji: str | None = None) -> Table:
    """Factory for a consistently themed :class:`rich.table.Table`.

    The caller adds rows and prints the table. Styling, header colour,
    and border characters live here so every ``list`` command renders
    the same way.
    """

    header = f"{emoji} {title}" if emoji else title
    table = Table(title=header, title_style="tessera.info", header_style="tessera.info")
    for col in columns:
        table.add_column(col)
    return table


def status_cell(label: str) -> Text:
    """Return a coloured :class:`rich.text.Text` for an ``OK/WARN/ERROR`` cell.

    Used by ``tessera doctor`` so every row's status column gets the
    same treatment. Unknown labels render in the dim style so unexpected
    values are visually distinct from the three known tokens.
    """

    style = {
        "OK": "tessera.success",
        "WARN": "tessera.warn",
        "ERROR": "tessera.error",
    }.get(label.upper(), "tessera.dim")
    badge_emoji = {
        "OK": EMOJI["ok"],
        "WARN": EMOJI["warn"],
        "ERROR": EMOJI["error"],
    }.get(label.upper(), "·")
    return Text(f"{badge_emoji} {label.upper()}", style=style)


__all__ = [
    "EMOJI",
    "console",
    "err_console",
    "error",
    "fail",
    "info",
    "kv_panel",
    "progress",
    "raw",
    "report_table",
    "status",
    "status_cell",
    "success",
    "warn",
]
