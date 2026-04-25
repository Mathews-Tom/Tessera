"""Smoke tests establishing the package imports and exposes its version."""

from __future__ import annotations

import re

import pytest

import tessera

# PEP 440 pre-release / dev / post markers. While the project is pre-1.0 the
# version always carries one of these (rcN, aN, bN, .devN, .postN). A bare
# `0.X.Y` final release would mean we forgot to prefix the next dev cycle.
_PEP440_PRERELEASE = re.compile(r"^0\.\d+\.\d+(rc|a|b|\.dev|\.post)\d+$")


@pytest.mark.unit
def test_package_exposes_version() -> None:
    assert isinstance(tessera.__version__, str)
    assert tessera.__version__ != ""


@pytest.mark.unit
def test_package_version_is_pep440_prerelease() -> None:
    assert _PEP440_PRERELEASE.match(tessera.__version__), (
        f"package __version__={tessera.__version__!r} must be a PEP 440 pre-release "
        "while we are pre-1.0 (rc / alpha / beta / .dev / .post)"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name",
    [
        "tessera.adapters",
        "tessera.auth",
        "tessera.cli",
        "tessera.daemon",
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
