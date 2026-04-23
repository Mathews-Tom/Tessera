"""B-RET-3 — cross-facet ``recall(facet_types=all)`` bundle-assembly latency.

Records end-to-end latency for the post-reframe cross-facet recall
primitive on a synthetic vault spanning every v0.1 facet type
(identity / preference / workflow / project / style). Uses
deterministic fake adapters so the measurement isolates the
bundle-assembly cost (per-facet-type candidate generation via
asyncio.gather + RRF + rerank + SWCR + MMR + token budget) from
provider-side embedding latency.

Per ADR 0010, ``assume_identity`` is retired; cross-facet context is
delivered by ``recall`` with ``facet_types`` defaulting to every type
the caller is scoped for. This harness exercises that code path.

The v0.1 DoD target (``docs/release-spec.md §Performance``) is
p50 < 1.5 s, p95 < 3 s at 10K facets on the reference hardware
baseline (M1 Pro). This harness uses a smaller vault with fake
adapters so the shape is reproducible offline in seconds.
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

import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.migration import bootstrap
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext, recall
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

HERE = Path(__file__).parent
RESULTS_DIR = HERE / "results"
DIM = 8
# Per-facet counts sized to cover the five v0.1 types with a realistic
# distribution: lots of small-grained project/style rows, fewer stable
# identity and preference rows. The total (2_000) stays small enough
# that the fake-adapter benchmark runs in seconds but large enough to
# stress candidate generation + RRF + rerank + SWCR + MMR.
N_PER_TYPE: dict[str, int] = {
    "identity": 20,
    "preference": 60,
    "workflow": 120,
    "project": 900,
    "style": 900,
}
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


def _seed_content(facet_type: str, index: int) -> str:
    """Produce a deterministic, facet-type-flavoured content string."""

    flavor = {
        "identity": "stable-for-years fact about the user",
        "preference": "stable-for-months behavioural rule",
        "workflow": "procedural pattern the user reuses",
        "project": "active work context the user is building",
        "style": "writing-voice sample in the user's register",
    }[facet_type]
    return f"{facet_type} row {index}: {flavor}"


def _scale(scale: int) -> dict[str, int]:
    """Return the per-type facet counts scaled by ``scale``.

    Scale 1 targets the reproducible fake-adapter default (2_000 total).
    Scale 5 targets the 10K finalisation run the v0.1 DoD links to.
    Non-integer multiples are not exposed — the ratios below are the
    realistic T-shape distribution called for in the plan.
    """

    return {ftype: count * scale for ftype, count in N_PER_TYPE.items()}


async def _run(*, scale: int = 1, trials: int = TRIALS) -> int:
    per_type = _scale(scale)
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
                # Stagger capture timestamps so ``captured_at DESC`` gives
                # the retrieval pipeline something non-trivial to rank.
                ts = now_base
                for ftype, count in per_type.items():
                    for i in range(count):
                        capture.capture(
                            vc.connection,
                            agent_id=agent_id,
                            facet_type=ftype,
                            content=_seed_content(ftype, i),
                            source_tool="bench",
                            captured_at=ts,
                        )
                        ts -= 60
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
                    facet_types=tuple(per_type.keys()),
                )
                # Warm-up: prime the reranker and the embedder cache
                # before the measured loop.
                await recall(ctx, query_text="bench warmup query")
                samples_ms: list[float] = []
                for i in range(trials):
                    start = time.perf_counter()
                    await recall(ctx, query_text=f"cross-facet bundle {i}")
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
            "n_per_type": dict(per_type),
            "n_facets_total": sum(per_type.values()),
            "dim": DIM,
            "trials": trials,
            "scale": scale,
            "adapters": "fake",
            "embedder": "hash-fake",
            "reranker": "length-fake",
            "tool_budget_tokens": 6000,
            "facet_types": list(per_type.keys()),
            "retrieval_mode": "swcr",
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


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b-ret-3")
    parser.add_argument(
        "--scale",
        type=int,
        default=1,
        help="per-type count multiplier (1 = 2K default, 5 = 10K DoD finalisation)",
    )
    parser.add_argument("--trials", type=int, default=TRIALS)
    args = parser.parse_args(argv)
    return asyncio.run(_run(scale=args.scale, trials=args.trials))


if __name__ == "__main__":
    raise SystemExit(_cli())
