"""B-RET-3 — assume_identity latency baseline.

Records end-to-end identity-bundle latency on a synthetic vault with
mixed facet types. Uses deterministic fake adapters so the measurement
isolates the bundle-assembly cost (per-role recall via asyncio.gather +
time-window filter + bundle budget) from provider-side embedding
latency. The v0.1 DoD target (docs/release-spec.md §Performance) is
p50 < 1.5 s, p95 < 3 s at 10K facets on M1 Pro; this first pass uses
a smaller vault with fake adapters so the harness is reproducible
offline and the shape is measurable in seconds, not minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
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

import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.identity.bundle import assume_identity
from tessera.migration import bootstrap
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

HERE = Path(__file__).parent
RESULTS_DIR = HERE / "results"
DIM = 8
N_STYLE = 500
N_EPISODIC = 1500
TRIALS = 100


class _HashEmbedder:
    name: ClassVar[str] = "fake"
    model_name: str = "hash-fake"
    dim: int = DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([digest[i] / 255.0 for i in range(self.dim)])
        return out

    async def health_check(self) -> None:
        return None


class _LengthReranker:
    name: ClassVar[str] = "fake"
    model_name: str = "length"

    async def score(
        self, query: str, passages: Sequence[str], *, seed: int | None = None
    ) -> list[float]:
        del query, seed
        return [1.0 / (1 + len(p)) for p in passages]

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
            cwd=HERE,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    return out.decode().strip()


async def _run() -> int:
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-ret-3.db"
        passphrase = b"b-ret-3-passphrase"
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
                embedder = _HashEmbedder()
                reranker = _LengthReranker()
                model = models_registry.register_embedding_model(
                    vc.connection, name="ollama", dim=DIM, activate=True
                )
                now_base = 1_700_000_000
                for i in range(N_STYLE):
                    capture.capture(
                        vc.connection,
                        agent_id=agent_id,
                        facet_type="style",
                        content=f"voice sample {i}: terse imperative code-first",
                        source_client="bench",
                        captured_at=now_base - i * 3600,
                    )
                for i in range(N_EPISODIC):
                    capture.capture(
                        vc.connection,
                        agent_id=agent_id,
                        facet_type="episodic",
                        content=f"event {i}: decided to ship and reviewed backlog",
                        source_client="bench",
                        captured_at=now_base - i * 600,
                    )
                while True:
                    stats = await embed_worker.run_pass(
                        vc.connection, embedder, active_model_id=model.id, batch_size=128
                    )
                    if stats.embedded == 0:
                        break
                ctx = PipelineContext(
                    conn=vc.connection,
                    embedder=embedder,
                    reranker=reranker,
                    active_model_id=model.id,
                    vec_table=models_registry.vec_table_name(model.id),
                    vault_id="B-RET-3",
                    agent_id=agent_id,
                    config=RetrievalConfig(
                        rerank_model="length",
                        mmr_lambda=0.7,
                        max_candidates=50,
                    ),
                    tool_budget_tokens=6000,
                    k=20,
                    facet_types=("style", "episodic"),
                )
                await assume_identity(ctx, now_epoch=now_base)  # warm-up
                samples_ms: list[float] = []
                for i in range(TRIALS):
                    start = time.perf_counter()
                    await assume_identity(
                        ctx,
                        now_epoch=now_base,
                        model_hint=f"bench-model-{i}",
                    )
                    samples_ms.append((time.perf_counter() - start) * 1000.0)

    metrics = {
        "p50_ms": statistics.median(samples_ms),
        "p95_ms": _percentile(samples_ms, 95),
        "p99_ms": _percentile(samples_ms, 99),
        "min_ms": min(samples_ms),
        "max_ms": max(samples_ms),
        "mean_ms": statistics.fmean(samples_ms),
    }
    payload = {
        "benchmark_id": "B-RET-3",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "n_style": N_STYLE,
            "n_episodic": N_EPISODIC,
            "n_facets_total": N_STYLE + N_EPISODIC,
            "dim": DIM,
            "trials": TRIALS,
            "adapters": "fake",
            "embedder": "hash-fake",
            "reranker": "length-fake",
            "tool_budget_tokens": 6000,
            "recent_window_hours": 168,
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
    print(
        f"p50={metrics['p50_ms']:.1f}ms p95={metrics['p95_ms']:.1f}ms "
        f"p99={metrics['p99_ms']:.1f}ms (DoD target: p50<1500ms, p95<3000ms)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
