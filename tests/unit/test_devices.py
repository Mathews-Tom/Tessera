"""Unit tests for torch device resolution."""

from __future__ import annotations

from typing import Any
from unittest.mock import _patch, patch

import pytest

from tessera.adapters.devices import ENV_OVERRIDE, detect_best_device


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)


def _patch_backends(
    *, cuda: bool, mps_available: bool, mps_built: bool
) -> tuple[_patch[Any], _patch[Any], _patch[Any]]:
    return (
        patch("tessera.adapters.devices.torch.cuda.is_available", return_value=cuda),
        patch(
            "tessera.adapters.devices.torch.backends.mps.is_available",
            return_value=mps_available,
        ),
        patch(
            "tessera.adapters.devices.torch.backends.mps.is_built",
            return_value=mps_built,
        ),
    )


def test_auto_prefers_cuda_when_available() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=True, mps_available=True, mps_built=True
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("auto") == "cuda"


def test_auto_falls_through_to_mps_when_no_cuda() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=False, mps_available=True, mps_built=True
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("auto") == "mps"


def test_auto_falls_through_to_cpu_when_no_accelerator() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=False, mps_available=False, mps_built=False
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("auto") == "cpu"


def test_mps_requires_both_available_and_built() -> None:
    # available but not built → treated as unavailable
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=False, mps_available=True, mps_built=False
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("auto") == "cpu"


def test_env_override_applied_only_when_explicit_is_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_OVERRIDE, "cpu")
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=True, mps_available=True, mps_built=True
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("auto") == "cpu"
        # Explicit caller value bypasses env override.
        assert detect_best_device("cuda") == "cuda"


def test_explicit_cuda_rejected_when_unavailable() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=False, mps_available=False, mps_built=False
    )
    with p_cuda, p_mps_avail, p_mps_built, pytest.raises(ValueError, match="cuda"):
        detect_best_device("cuda")


def test_explicit_mps_rejected_when_unavailable() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=False, mps_available=False, mps_built=False
    )
    with p_cuda, p_mps_avail, p_mps_built, pytest.raises(ValueError, match="mps"):
        detect_best_device("mps")


def test_explicit_cpu_always_accepted() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=False, mps_available=False, mps_built=False
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("cpu") == "cpu"


def test_cuda_index_form_accepted() -> None:
    p_cuda, p_mps_avail, p_mps_built = _patch_backends(
        cuda=True, mps_available=False, mps_built=False
    )
    with p_cuda, p_mps_avail, p_mps_built:
        assert detect_best_device("cuda:0") == "cuda:0"
        assert detect_best_device("cuda:1") == "cuda:1"


def test_invalid_device_string_rejected() -> None:
    with pytest.raises(ValueError, match="invalid device"):
        detect_best_device("tpu")


def test_env_override_invalid_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OVERRIDE, "rocm")
    with pytest.raises(ValueError, match="invalid device"):
        detect_best_device("auto")
