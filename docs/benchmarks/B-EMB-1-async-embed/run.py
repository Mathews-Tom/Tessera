"""B-EMB-1 — async embed throughput baseline (mocked embedder).

Measures how quickly the embed worker drains a pending backlog against
a zero-latency fake embedder. This isolates the sqlite-vec write path
and the worker's state-machine overhead from provider latency, so later
measurements with a real Ollama at 10K facets attribute the delta to
the provider rather than the storage layer.

The full P3 DoD metric — "no facet lingers pending > 10 min; Ollama
restart recovery within 60 s" — is operational and will be measured
against a live Ollama restart sequence, not this baseline.
"""

from __future__ import annotations

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

# Registering the Ollama embedder name satisfies the models_registry adapter
# check; the throughput test uses a fake embedder directly.
import tessera.adapters.ollama_embedder  # noqa: F401
from tessera.adapters import models_registry
from tessera.migration import bootstrap
from tessera.retrieval import embed_worker
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

RESULTS_DIR = Path(__file__).parent / "results"
FACETS = 500
DIM = 8


class _FakeEmbedder:
    name: ClassVar[str] = "fake"
    model_name: str = "fake"
    dim: int = DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Zero-latency stand-in — throughput reflects the worker path only.
        return [[0.0] * DIM for _ in texts]

    async def health_check(self) -> None:
        return None


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


async def _drain(conn: Any, model_id: int) -> tuple[int, float]:
    embedder = _FakeEmbedder()
    start = time.perf_counter()
    embedded_total = 0
    passes = 0
    while True:
        stats = await embed_worker.run_pass(conn, embedder, active_model_id=model_id, batch_size=32)
        embedded_total += stats.embedded
        passes += 1
        if stats.embedded == 0:
            break
    elapsed = time.perf_counter() - start
    return embedded_total, elapsed


async def _run() -> int:
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-emb-1.db"
        passphrase = b"b-emb-1-bench-passphrase"
        salt = new_salt()
        with derive_key(passphrase, salt) as key:
            bootstrap(vault_path, key)
            with VaultConnection.open(vault_path, key) as vc:
                vc.connection.execute(
                    "INSERT INTO agents(external_id, name, created_at) VALUES ('01B', 'b', 0)"
                )
                agent_id = int(
                    vc.connection.execute(
                        "SELECT id FROM agents WHERE external_id='01B'"
                    ).fetchone()[0]
                )
                model = models_registry.register_embedding_model(
                    vc.connection, name="ollama", dim=DIM, activate=True
                )
                capture_start = time.perf_counter()
                for i in range(FACETS):
                    capture.capture(
                        vc.connection,
                        agent_id=agent_id,
                        facet_type="episodic",
                        content=f"content-{i}",
                        source_client="bench",
                    )
                capture_elapsed = time.perf_counter() - capture_start

                embedded, embed_elapsed = await _drain(vc.connection, model.id)

    metrics = {
        "facets": FACETS,
        "dim": DIM,
        "capture_elapsed_seconds": capture_elapsed,
        "capture_throughput_per_sec": FACETS / capture_elapsed,
        "embed_elapsed_seconds": embed_elapsed,
        "embed_throughput_per_sec": embedded / embed_elapsed,
        "embedded_total": embedded,
    }
    payload = {
        "benchmark_id": "B-EMB-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "facets": FACETS,
            "dim": DIM,
            "embedder": "fake-zero-latency",
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
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))


_ = statistics  # reserved for later percentile tracking once we run at scale
