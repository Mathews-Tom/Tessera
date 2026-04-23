"""Torch device detection for in-process adapters.

The cross-encoder reranker runs through PyTorch; sentence-transformers
accepts a ``device`` string that covers CUDA, Apple Silicon Metal (MPS),
and CPU through one API. Detection priority is CUDA > MPS > CPU to match
hardware capacity.

``TESSERA_RERANK_DEVICE`` overrides auto-detection for users who need a
specific backend — the primary use case is forcing CPU for cross-run
bit-identical determinism, which MPS and CUDA do not guarantee across
daemon restarts. Invalid values fail loud at resolution time.
"""

from __future__ import annotations

import os
from typing import Final

import torch

ENV_OVERRIDE: Final[str] = "TESSERA_RERANK_DEVICE"

_VALID_EXPLICIT = frozenset({"auto", "cpu", "mps", "cuda"})


def detect_best_device(explicit: str = "auto") -> str:
    """Resolve the reranker device string.

    ``explicit`` accepts ``"auto"`` (detect best), ``"cpu"``, ``"mps"``,
    ``"cuda"``, or any CUDA index form (``"cuda:0"``, ``"cuda:1"``). The
    environment variable :data:`ENV_OVERRIDE` takes precedence over
    ``explicit`` when ``explicit == "auto"``; an explicit non-auto value
    passed by the caller is never overridden silently.

    Raises :class:`ValueError` when an explicit device is unavailable on
    the current host — per the no-fallback policy, a user who asked for
    MPS on a non-Metal box should see the error, not a silent CPU
    downgrade.
    """

    env = os.environ.get(ENV_OVERRIDE)
    resolved = env if explicit == "auto" and env else explicit

    if resolved == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available() and torch.backends.mps.is_built():
            return "mps"
        return "cpu"

    base = resolved.split(":", 1)[0]
    if base not in _VALID_EXPLICIT - {"auto"}:
        raise ValueError(
            f"invalid device {resolved!r}; expected one of cpu, mps, cuda, cuda:<index>"
        )
    if base == "cuda" and not torch.cuda.is_available():
        raise ValueError(
            f"device {resolved!r} requested but torch.cuda.is_available() is False"
        )
    if base == "mps" and not (
        torch.backends.mps.is_available() and torch.backends.mps.is_built()
    ):
        raise ValueError(
            f"device {resolved!r} requested but torch MPS backend is unavailable on this host"
        )
    return resolved


__all__ = ["ENV_OVERRIDE", "detect_best_device"]
