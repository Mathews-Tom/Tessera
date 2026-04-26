"""DaemonConfig resolution: env, defaults, overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.daemon.config import DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT, resolve_config


@pytest.mark.unit
def test_resolve_config_uses_defaults_when_env_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in (
        "TESSERA_VAULT",
        "TESSERA_HTTP_HOST",
        "TESSERA_HTTP_PORT",
        "TESSERA_RERANKER",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = resolve_config()
    assert config.http_host == DEFAULT_HTTP_HOST
    assert config.http_port == DEFAULT_HTTP_PORT
    assert config.reranker_model == "Xenova/ms-marco-MiniLM-L-12-v2"
    assert config.socket_path.name == "tessera.sock"
    assert config.allowed_origins == frozenset({"http://localhost", "http://127.0.0.1", "null"})


@pytest.mark.unit
def test_resolve_config_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERA_HTTP_HOST", "127.0.0.2")
    monkeypatch.setenv("TESSERA_HTTP_PORT", "6000")
    monkeypatch.setenv("TESSERA_RERANKER", "Xenova/ms-marco-MiniLM-L-6-v2")
    config = resolve_config()
    assert config.http_host == "127.0.0.2"
    assert config.http_port == 6000
    assert config.reranker_model == "Xenova/ms-marco-MiniLM-L-6-v2"


@pytest.mark.unit
def test_resolve_config_caller_overrides_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TESSERA_HTTP_PORT", "9999")
    config = resolve_config(http_port=1234, vault_path=tmp_path / "v.db")
    assert config.http_port == 1234
    assert config.vault_path == tmp_path / "v.db"


@pytest.mark.unit
def test_resolve_config_uses_xdg_runtime_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    config = resolve_config()
    assert config.socket_path.parent == tmp_path / "tessera"
