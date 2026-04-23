"""Daemon configuration resolved from env + CLI flags.

One place to resolve every filesystem, network, and runtime default.
The daemon reads exactly one :class:`DaemonConfig` at startup — the
config is immutable thereafter, which makes "reload config" a restart
rather than a runtime concept, matching the rest of v0.1's
single-process shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_HTTP_PORT: Final[int] = 5710
DEFAULT_HTTP_HOST: Final[str] = "127.0.0.1"
_DEFAULT_VAULT_DIR: Final[str] = "~/.tessera"
_SOCKET_FILENAME: Final[str] = "tessera.sock"
_LOG_FILENAME: Final[str] = "tesserad.log"
_PID_FILENAME: Final[str] = "tesserad.pid"
_EVENTS_DB_FILENAME: Final[str] = "events.db"


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Everything the daemon needs to start.

    ``passphrase`` is only populated when the caller has already
    materialised the user's passphrase (CLI prompt, env var). The
    daemon never reads the passphrase off disk at startup; an unlocked
    vault requires the operator to either pass one at ``start`` or
    respond to a keyring prompt per docs/system-design.md §Unlock flow.
    """

    vault_path: Path
    http_host: str
    http_port: int
    socket_path: Path
    log_path: Path
    pid_path: Path
    events_db_path: Path
    allowed_origins: frozenset[str]
    ollama_host: str
    reranker_model: str
    passphrase: bytes | None = None


def resolve_config(
    *,
    vault_path: Path | None = None,
    http_host: str | None = None,
    http_port: int | None = None,
    socket_path: Path | None = None,
    ollama_host: str | None = None,
    reranker_model: str | None = None,
    passphrase: bytes | None = None,
) -> DaemonConfig:
    """Merge caller overrides, env vars, and built-in defaults."""

    vault = vault_path or Path(os.environ.get("TESSERA_VAULT", "")) or _default_vault_path()
    host = http_host or os.environ.get("TESSERA_HTTP_HOST", DEFAULT_HTTP_HOST)
    port = http_port or int(os.environ.get("TESSERA_HTTP_PORT", str(DEFAULT_HTTP_PORT)))
    sock = socket_path or _default_socket_path()
    ollama = ollama_host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    reranker = reranker_model or os.environ.get(
        "TESSERA_RERANKER", "cross-encoder/ms-marco-MiniLM-L-6-v2"
    )
    runtime_dir = _runtime_dir()
    return DaemonConfig(
        vault_path=vault,
        http_host=host,
        http_port=port,
        socket_path=sock,
        log_path=runtime_dir / _LOG_FILENAME,
        pid_path=runtime_dir / _PID_FILENAME,
        events_db_path=Path(_DEFAULT_VAULT_DIR).expanduser() / _EVENTS_DB_FILENAME,
        # Localhost-only bind already blocks public access; the Origin
        # allowlist further rejects browser-driven requests that
        # hijack ambient-authority DNS rebind vectors.
        allowed_origins=frozenset({"http://localhost", "http://127.0.0.1", "null"}),
        ollama_host=ollama,
        reranker_model=reranker,
        passphrase=passphrase,
    )


def _default_vault_path() -> Path:
    return Path(_DEFAULT_VAULT_DIR).expanduser() / "vault.db"


def _default_socket_path() -> Path:
    return _runtime_dir() / _SOCKET_FILENAME


def _runtime_dir() -> Path:
    """Pick XDG_RUNTIME_DIR when present, else ``~/.tessera/run``.

    macOS does not set ``XDG_RUNTIME_DIR`` by default, and the launchd
    per-user cache dir is not documented stable across macOS versions
    — ``~/.tessera/run`` is the portable fallback that stays within
    the user's home and survives reboots without relying on
    platform-specific state.
    """

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "tessera"
    return Path(_DEFAULT_VAULT_DIR).expanduser() / "run"


__all__ = [
    "DEFAULT_HTTP_HOST",
    "DEFAULT_HTTP_PORT",
    "DaemonConfig",
    "resolve_config",
]
