"""B-RET-2 — retrieval latency at N facets.

Records end-to-end pipeline latency (BM25 + dense + RRF + rerank + SWCR +
MMR + budget) against a fresh encrypted vault preloaded with ``--n-facets``
synthetic project facets. Uses a deterministic hash-based fake embedder and
an in-process score-by-length fake reranker so the measurement isolates
the pipeline cost from provider-side embedding latency.

The v0.1 DoD targets (``docs/release-spec.md §Performance``) are p50
median < 500 ms and p95 < 1 s at 10K on the reference hardware baseline
(M1 Pro). Run with ``--n-facets 10000`` to produce the finalisation
result the DoD links to.
"""

from __future__ import annotations

import argparse
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

# Registering the Ollama adapter name satisfies models_registry's adapter check.
import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.migration import bootstrap
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext, recall
from tessera.retrieval.seed import DEFAULT_RETRIEVAL_MODE, RetrievalConfig, RetrievalMode
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

RESULTS_DIR = Path(__file__).parent / "results"
DIM = 8
DEFAULT_TRIALS = 100


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
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    return out.decode().strip()


async def _run(*, n_facets: int, trials: int, retrieval_mode: RetrievalMode) -> int:
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-ret-2.db"
        passphrase = b"b-ret-2-passphrase"
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
                for i in range(n_facets):
                    capture.capture(
                        vc.connection,
                        agent_id=agent_id,
                        facet_type="project",
                        content=f"project note {i} about retrieval latency on synthetic vault",
                        source_tool="bench",
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
                    vault_id="01VAULT",
                    agent_id=agent_id,
                    config=RetrievalConfig(
                        rerank_model="length",
                        mmr_lambda=0.7,
                        max_candidates=50,
                        retrieval_mode=retrieval_mode,
                    ),
                    tool_budget_tokens=2000,
                    k=5,
                    facet_types=("project",),
                )
                # Warm-up call, discarded.
                await recall(ctx, query_text="warm-up")
                samples_ms: list[float] = []
                for i in range(trials):
                    start = time.perf_counter()
                    await recall(ctx, query_text=f"retrieval query variant {i}")
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
        "benchmark_id": "B-RET-2",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "facets": n_facets,
            "dim": DIM,
            "trials": trials,
            "embedder": "hash-fake",
            "reranker": "length-fake",
            "retrieval_mode": retrieval_mode,
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
        f"p50={metrics['p50_ms']:.1f}ms p95={metrics['p95_ms']:.1f}ms "
        f"p99={metrics['p99_ms']:.1f}ms "
        f"(DoD target at 10K: p50<500ms, p95<1000ms)"
    )
    return 0


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b-ret-2")
    parser.add_argument("--n-facets", type=int, default=1000)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument(
        "--retrieval-mode",
        choices=("rrf_only", "rerank_only", "swcr"),
        default=DEFAULT_RETRIEVAL_MODE,
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            n_facets=args.n_facets,
            trials=args.trials,
            retrieval_mode=args.retrieval_mode,
        )
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
