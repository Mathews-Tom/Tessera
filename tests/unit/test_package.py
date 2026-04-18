"""Smoke tests establishing the package imports and exposes its version."""

from __future__ import annotations

import pytest

import tessera


@pytest.mark.unit
def test_package_exposes_version() -> None:
    assert isinstance(tessera.__version__, str)
    assert tessera.__version__ != ""


@pytest.mark.unit
def test_package_version_is_pep440_pre_release() -> None:
    parts = tessera.__version__.split(".")
    assert parts[0] == "0"
    assert "dev" in tessera.__version__


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name",
    [
        "tessera.adapters",
        "tessera.auth",
        "tessera.cli",
        "tessera.daemon",
        "tessera.identity",
        "tessera.mcp_surface",
        "tessera.migration",
        "tessera.observability",
        "tessera.retrieval",
        "tessera.vault",
    ],
)
def test_submodule_imports(module_name: str) -> None:
    module = __import__(module_name, fromlist=["__name__"])
    assert module.__name__ == module_name
