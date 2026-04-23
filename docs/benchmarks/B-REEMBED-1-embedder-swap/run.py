"""B-REEMBED-1 — embedder-swap wall time.

Measures the end-to-end cost of rotating the active embedding model:
register the new model, flip ``is_active``, mark every existing
facet ``embed_status='pending'``, drain the embed worker against the
new adapter. Records total wall clock plus per-batch latency.

The v0.1 DoD target (``docs/release-spec.md §v0.1 DoD``) is 10K
facets re-embedded in under 10 minutes on the M1 Pro reference
baseline. The harness ships with fake adapters so the measurement
isolates the storage + worker costs from provider throughput; a
live-Ollama rerun happens in P14 hardening.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar

# Module-level side effect: registers the "ollama" / "openai" names in
# models_registry so register_embedding_model below accepts them. The
# modules do not need to be accessed by name; the import executes
# their registration decorators.
import tessera.adapters.ollama_embedder
import tessera.adapters.openai_embedder  # noqa: F401
from tessera.adapters import models_registry
from tessera.migration import bootstrap
from tessera.retrieval import embed_worker
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

RESULTS_DIR = Path(__file__).parent / "results"
DEFAULT_FACETS = 10_000
DIM_A = 8
DIM_B = 16


class _FakeEmbedder:
    """Zero-latency embedder so the measurement reflects storage + worker cost only."""

    name: ClassVar[str] = "fake"

    def __init__(self, dim: int, model_name: str) -> None:
        self.dim = dim
        self.model_name = model_name

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Tiny stable vector — different per model so the vec table isn't
        # trivially re-used. The actual values do not matter for the
        # wall-time measurement.
        return [[float((hash(t) + i) % 17) / 17.0 for i in range(self.dim)] for t in texts]

    async def health_check(self) -> None:
        return None


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


async def _run(*, facets: int, batch_size: int) -> int:
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-reembed-1.db"
        salt = new_salt()
        with derive_key(bytearray(b"b-reembed-1-bench"), salt) as key:
            bootstrap(vault_path, key)
            with VaultConnection.open(vault_path, key) as vc:
                vc.connection.execute(
                    "INSERT INTO agents(external_id, name, created_at) VALUES ('01BENCH', 'bench', 0)"
                )
                agent_id = int(
                    vc.connection.execute(
                        "SELECT id FROM agents WHERE external_id='01BENCH'"
                    ).fetchone()[0]
                )
                model_a = models_registry.register_embedding_model(
                    vc.connection, name="ollama", dim=DIM_A, activate=True
                )
                embedder_a = _FakeEmbedder(dim=DIM_A, model_name="fake-a")
                for i in range(facets):
                    capture.capture(
                        vc.connection,
                        agent_id=agent_id,
                        facet_type="project",
                        content=f"reembed bench content {i}",
                        source_tool="bench",
                    )
                while True:
                    stats = await embed_worker.run_pass(
                        vc.connection,
                        embedder_a,
                        active_model_id=model_a.id,
                        batch_size=batch_size,
                    )
                    if stats.embedded == 0:
                        break
                # Register model B with a distinct dim so a fresh vec
                # table is created; activate it so the worker knows to
                # route writes to the new model.
                model_b = models_registry.register_embedding_model(
                    vc.connection, name="openai", dim=DIM_B, activate=True
                )
                embedder_b = _FakeEmbedder(dim=DIM_B, model_name="fake-b")
                # Mark every existing facet pending so the worker
                # re-embeds them against model B.
                vc.connection.execute(
                    """
                    UPDATE facets
                    SET embed_status = 'pending',
                        embed_attempts = 0,
                        embed_last_attempt_at = NULL,
                        embed_last_error = NULL
                    WHERE is_deleted = 0
                    """
                )
                batch_ms: list[float] = []
                wall_start = time.perf_counter()
                while True:
                    t0 = time.perf_counter()
                    stats = await embed_worker.run_pass(
                        vc.connection,
                        embedder_b,
                        active_model_id=model_b.id,
                        batch_size=batch_size,
                    )
                    batch_ms.append((time.perf_counter() - t0) * 1000.0)
                    if stats.embedded == 0:
                        break
                wall_ms = (time.perf_counter() - wall_start) * 1000.0

                final_pending = int(
                    vc.connection.execute(
                        "SELECT COUNT(*) FROM facets "
                        "WHERE is_deleted = 0 AND embed_status = 'pending'"
                    ).fetchone()[0]
                )
                final_embedded = int(
                    vc.connection.execute(
                        "SELECT COUNT(*) FROM facets "
                        "WHERE is_deleted = 0 AND embed_status = 'embedded'"
                    ).fetchone()[0]
                )

    metrics = {
        "wall_ms": wall_ms,
        "batch_p50_ms": statistics.median(batch_ms) if batch_ms else 0.0,
        "batch_p95_ms": _percentile(batch_ms, 95),
        "batch_p99_ms": _percentile(batch_ms, 99),
        "batch_count": len(batch_ms),
        "throughput_facets_per_sec": facets * 1000.0 / wall_ms if wall_ms > 0 else 0.0,
        "final_pending": final_pending,
        "final_embedded": final_embedded,
    }
    payload = {
        "benchmark_id": "B-REEMBED-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "facets": facets,
            "batch_size": batch_size,
            "dim_a": DIM_A,
            "dim_b": DIM_B,
            "adapters": "fake",
            "embedder_a": "fake-a",
            "embedder_b": "fake-b",
        },
        "metrics": metrics,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"{stamp}.json"
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote {out}\n"
        f"wall={metrics['wall_ms']:.1f}ms throughput={metrics['throughput_facets_per_sec']:.1f}/s "
        f"(DoD target at 10K: wall<600_000ms / 10min on live Ollama)"
    )
    if final_pending:
        print(f"WARNING: {final_pending} facets still pending", file=sys.stderr)
    return 0


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b-reembed-1")
    parser.add_argument("--facets", type=int, default=DEFAULT_FACETS)
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args(argv)
    return asyncio.run(_run(facets=args.facets, batch_size=args.batch_size))


if __name__ == "__main__":
    raise SystemExit(_cli())
