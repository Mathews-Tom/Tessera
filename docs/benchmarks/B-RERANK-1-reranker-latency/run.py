"""B-RERANK-1 — cross-encoder reranker latency baseline.

Measures the per-query latency of scoring (query, passage) pairs on the
reference reranker (``cross-encoder/ms-marco-MiniLM-L-6-v2``) across a
representative candidate-set size (top-50) and a smaller diagnostic size
(top-10). First run downloads model weights (~90 MB) into the HuggingFace
cache; subsequent runs hit the local cache.

The full B-RERANK-1 cross-platform matrix (M1/M2/M3 Pro, Linux x86,
Windows) is finalised in P12. This harness establishes the measurement
shape and records a baseline on the contributor's machine.
"""

from __future__ import annotations

import asyncio
import json
import platform
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch

from tessera.adapters.st_reranker import SentenceTransformersReranker

RESULTS_DIR = Path(__file__).parent / "results"
TRIALS = 100
QUERY = "how does Tessera store agent identity across substrate changes?"
BASE_PASSAGE = (
    "Tessera is a substrate-independent identity layer for AI agents. "
    "It stores an agent's identity in a single-file encrypted SQLite vault "
    "and serves it to any MCP-capable agent via scoped capability tokens. "
)
CANDIDATE_SIZES = (10, 50)


def _passages(n: int) -> list[str]:
    return [f"{BASE_PASSAGE} candidate={i}" for i in range(n)]


async def _time_scoring(
    reranker: SentenceTransformersReranker,
    size: int,
    trials: int,
) -> dict[str, float]:
    passages = _passages(size)
    # Warm-up uses min 2 passages: some torch arm64 builds SIGBUS on batch
    # size 1 forward passes (see st_reranker.health_check comment).
    await reranker.score(QUERY, passages[:2])
    loop = asyncio.get_running_loop()
    samples_ms: list[float] = []
    for _ in range(trials):
        start = loop.time()
        await reranker.score(QUERY, passages, seed=0)
        samples_ms.append((loop.time() - start) * 1000.0)
    return {
        "p50_ms": statistics.median(samples_ms),
        "p95_ms": _percentile(samples_ms, 95),
        "p99_ms": _percentile(samples_ms, 99),
        "min_ms": min(samples_ms),
        "max_ms": max(samples_ms),
        "mean_ms": statistics.fmean(samples_ms),
    }


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = round((pct / 100.0) * (len(ordered) - 1))
    return ordered[idx]


def _env_block() -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "device": "cpu",
        "tessera_sha": _git_sha(),
    }


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    return out.decode().strip()


def _result_path() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return RESULTS_DIR / f"{stamp}.json"


async def _run() -> int:
    reranker = SentenceTransformersReranker()
    await reranker.health_check()
    metrics: dict[str, Any] = {}
    for size in CANDIDATE_SIZES:
        metrics[f"top_{size}"] = await _time_scoring(reranker, size, TRIALS)
    payload = {
        "benchmark_id": "B-RERANK-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
            "candidate_sizes": list(CANDIDATE_SIZES),
            "trials_per_size": TRIALS,
        },
        "metrics": metrics,
    }
    out = _result_path()
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
