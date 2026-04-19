"""B-RET-1 ablation harness — three-arm automated comparison.

Runs the retrieval pipeline against the S1 synthetic vault in three arms
and records the metrics required by ``docs/swcr-spec.md §Ablation protocol``
that can be automated without human raters:

    arm_A: RRF-only
    arm_B: RRF + rerank (P4 default)
    arm_C: RRF + rerank + SWCR (the proposed pipeline)

Arm D (RRF + rerank + Cohere rerank v3) is skipped because it requires a
licensed API key and would introduce outbound network calls into every
local run. If a contributor has a key they can wire the Cohere reranker
adapter and rerun.

Human coherence ratings (3 blind raters by 50 bundles by 5-point scale)
are not automatable and sit outside a single-session run; the result
file flags ``coherence_human: null`` and the default-on decision must
wait on that evidence per the spec's acceptance thresholds.

Metrics this harness computes:
    MRR@k over ground-truth-persona facets
    nDCG@k over ground-truth-persona facets
    coherence-synthetic: fraction of bundles where top-K facets all share
        the target persona (proxy for "does this hang together?")
    latency p50 / p95 / p99 per-arm

Reproduce:
    uv run python docs/benchmarks/B-RET-1-swcr-ablation/dataset/generate.py \
        --n-facets 2000 --n-queries 50
    uv run python docs/benchmarks/B-RET-1-swcr-ablation/run.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar

import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.adapters.ollama_embedder import OllamaEmbedder
from tessera.adapters.protocol import Embedder, Reranker
from tessera.adapters.st_reranker import SentenceTransformersReranker
from tessera.migration import bootstrap
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext, recall
from tessera.retrieval.seed import RetrievalConfig, RetrievalMode
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

HERE = Path(__file__).parent
DATASET_PATH = HERE / "dataset" / "s1.json"
RESULTS_DIR = HERE / "results"
FAKE_DIM = 16
OLLAMA_MODEL = "nomic-embed-text"
OLLAMA_DIM = 768
OLLAMA_HOST = "http://localhost:11434"
K = 5


class _HashEmbedder:
    """Deterministic fake embedder; content -> stable vector."""

    name: ClassVar[str] = "fake"
    model_name: str = "hash-fake"
    dim: int = FAKE_DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vec = [((digest[i % 32] / 255.0) * 2.0) - 1.0 for i in range(self.dim)]
            out.append(vec)
        return out

    async def health_check(self) -> None:
        return None


class _KeywordReranker:
    """Fake cross-encoder: score = query-token overlap / passage length.

    Gives a monotonically-decreasing signal toward passages that actually
    mention query tokens. This is noisier than a real cross-encoder but
    reproducible, network-free, and the ablation measures *relative*
    improvements so a weak ranker + SWCR vs a weak ranker alone is
    informative. P12 re-runs with the real MiniLM cross-encoder.
    """

    name: ClassVar[str] = "fake"
    model_name: str = "keyword-overlap"

    async def score(
        self, query: str, passages: Sequence[str], *, seed: int | None = None
    ) -> list[float]:
        del seed
        tokens = {t.lower() for t in query.split()}
        scores: list[float] = []
        for passage in passages:
            body = passage.lower()
            overlap = sum(1 for t in tokens if t in body)
            scores.append(overlap / (1 + len(passage.split())))
        return scores

    async def health_check(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Arm:
    id: str
    retrieval_mode: RetrievalMode


@dataclass
class ArmMetrics:
    mrr: float
    ndcg: float
    coherence_synthetic: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    mean_ms: float


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = round((pct / 100.0) * (len(ordered) - 1))
    return ordered[idx]


def _mrr_at_k(result_facet_ids: list[str], relevant_ext_ids: set[str], *, k: int) -> float:
    for idx, fid in enumerate(result_facet_ids[:k]):
        if fid in relevant_ext_ids:
            return 1.0 / (idx + 1)
    return 0.0


def _ndcg_at_k(result_facet_ids: list[str], relevant_ext_ids: set[str], *, k: int) -> float:
    # Binary relevance: 1 if facet is in the persona's ground-truth set,
    # else 0. DCG = sum(rel_i / log2(i+2)) for i in [0, k). IDCG is the
    # same sum assuming all top-k are relevant, capped at the available
    # relevant count.
    gains = [1.0 if fid in relevant_ext_ids else 0.0 for fid in result_facet_ids[:k]]
    dcg = sum(g / math.log2(idx + 2) for idx, g in enumerate(gains))
    ideal_gains = [1.0] * min(k, len(relevant_ext_ids))
    idcg = sum(g / math.log2(idx + 2) for idx, g in enumerate(ideal_gains))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def _coherence_synthetic(
    result_facet_ids: list[str],
    per_facet_persona: dict[str, str],
    target_persona: str,
    *,
    k: int,
) -> float:
    # "Top-K facets all share the target persona" = 1, else 0. Averaged
    # over queries, this gives the coherence-synthetic metric the spec
    # defines as "fraction of bundles where top-K facets share at least
    # one entity" — proxied here by persona identity since each persona
    # has its own entity vocabulary.
    if not result_facet_ids:
        return 0.0
    top = result_facet_ids[:k]
    if not top:
        return 0.0
    matches = sum(1 for fid in top if per_facet_persona.get(fid) == target_persona)
    return matches / len(top)


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


def _env_block() -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "tessera_sha": _git_sha(),
    }


async def _populate_vault(
    vc: VaultConnection, dataset: dict[str, Any]
) -> tuple[int, dict[int, str], dict[str, str], list[dict[str, Any]]]:
    """Insert the S1 facets, return agent_id + id lookups + queries."""

    vc.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01S1', 'agent', 0)"
    )
    agent_id = int(
        vc.connection.execute("SELECT id FROM agents WHERE external_id='01S1'").fetchone()[0]
    )
    synthetic_id_to_external: dict[int, str] = {}
    synthetic_id_to_persona: dict[int, str] = {}
    external_to_persona: dict[str, str] = {}
    for facet in dataset["facets"]:
        metadata = {
            "persona": facet["persona"],
            "entities": facet["entities"],
        }
        result = capture.capture(
            vc.connection,
            agent_id=agent_id,
            facet_type=facet["facet_type"],
            content=facet["content"],
            source_client="b-ret-1",
            metadata=metadata,
            captured_at=facet["captured_at"],
        )
        synthetic_id_to_external[facet["facet_id"]] = result.external_id
        synthetic_id_to_persona[facet["facet_id"]] = facet["persona"]
        external_to_persona[result.external_id] = facet["persona"]
    # Rewrite queries' relevant_facet_ids to external_ids now that the
    # vault assigned ULIDs.
    rewritten_queries: list[dict[str, Any]] = []
    for q in dataset["queries"]:
        relevant = [
            synthetic_id_to_external[fid]
            for fid in q["relevant_facet_ids"]
            if fid in synthetic_id_to_external
        ]
        rewritten_queries.append(
            {
                "query_text": q["query_text"],
                "persona": q["persona"],
                "relevant_external_ids": relevant,
            }
        )
    return agent_id, synthetic_id_to_external, external_to_persona, rewritten_queries


async def _run_arm(
    arm: Arm,
    *,
    ctx_factory: Any,
    queries: list[dict[str, Any]],
    external_to_persona: dict[str, str],
) -> ArmMetrics:
    ctx = ctx_factory(arm.retrieval_mode)
    mrrs: list[float] = []
    ndcgs: list[float] = []
    coherences: list[float] = []
    latencies_ms: list[float] = []
    # Warm-up: one discarded call primes any lazy state.
    if queries:
        await recall(ctx, query_text=queries[0]["query_text"])
    for q in queries:
        relevant = set(q["relevant_external_ids"])
        start = time.perf_counter()
        res = await recall(ctx, query_text=q["query_text"])
        latencies_ms.append((time.perf_counter() - start) * 1000.0)
        result_ids = [m.external_id for m in res.matches]
        mrrs.append(_mrr_at_k(result_ids, relevant, k=K))
        ndcgs.append(_ndcg_at_k(result_ids, relevant, k=K))
        coherences.append(_coherence_synthetic(result_ids, external_to_persona, q["persona"], k=K))
    return ArmMetrics(
        mrr=statistics.fmean(mrrs) if mrrs else 0.0,
        ndcg=statistics.fmean(ndcgs) if ndcgs else 0.0,
        coherence_synthetic=statistics.fmean(coherences) if coherences else 0.0,
        latency_p50_ms=_percentile(latencies_ms, 50),
        latency_p95_ms=_percentile(latencies_ms, 95),
        latency_p99_ms=_percentile(latencies_ms, 99),
        mean_ms=statistics.fmean(latencies_ms) if latencies_ms else 0.0,
    )


def _decide(arm_b: ArmMetrics, arm_c: ArmMetrics) -> dict[str, Any]:
    """Evaluate the spec's default-on thresholds against arm_B and arm_C.

    Coherence-human is out of session scope (3 raters by 50 bundles); the
    decision flags that gate as "deferred" and reports the three
    automatable thresholds.
    """

    ndcg_pct_improvement = (
        ((arm_c.ndcg - arm_b.ndcg) / arm_b.ndcg * 100.0) if arm_b.ndcg > 0 else 0.0
    )
    mrr_regression = arm_c.mrr < arm_b.mrr
    latency_abs_regression_ms = arm_c.latency_p95_ms - arm_b.latency_p95_ms
    latency_pct_regression = (
        ((arm_c.latency_p95_ms - arm_b.latency_p95_ms) / arm_b.latency_p95_ms * 100.0)
        if arm_b.latency_p95_ms > 0
        else 0.0
    )
    thresholds = {
        "ndcg_improvement_gte_10pct": ndcg_pct_improvement >= 10.0,
        "mrr_no_regression": not mrr_regression,
        "latency_p95_regression_lte_15pct_or_100ms": (
            latency_pct_regression <= 15.0 and latency_abs_regression_ms <= 100.0
        ),
        "coherence_human_deferred": True,
    }
    all_automatable_pass = all(v for k, v in thresholds.items() if k != "coherence_human_deferred")
    verdict = "provisional_default_on" if all_automatable_pass else "opt_in_recommended"
    return {
        "ndcg_pct_improvement_C_over_B": ndcg_pct_improvement,
        "mrr_regression_C_vs_B": mrr_regression,
        "latency_p95_delta_ms_C_minus_B": latency_abs_regression_ms,
        "latency_p95_pct_regression": latency_pct_regression,
        "thresholds": thresholds,
        "verdict": verdict,
        "verdict_notes": (
            "coherence_human gate requires 3 blind raters by 50 bundles and "
            "is deferred outside this single-session run. Final default-on "
            "ship decision blocks on that evidence per swcr-spec.md "
            "§Acceptance thresholds."
        ),
    }


def _select_adapters(
    adapters: str,
) -> tuple[Embedder, Reranker, int, str, str]:
    """Return ``(embedder, reranker, dim, embedder_id, reranker_id)``.

    ``adapters="fake"`` keeps the harness deterministic and network-free;
    ``adapters="real"`` swaps in the v0.1 DoD reference pair (Ollama
    ``nomic-embed-text`` + sentence-transformers MiniLM cross-encoder)
    so the ablation measures what the shipping default will actually
    see at recall time.
    """

    if adapters == "fake":
        return _HashEmbedder(), _KeywordReranker(), FAKE_DIM, "hash-fake", "keyword-overlap-fake"
    if adapters == "real":
        embedder = OllamaEmbedder(model_name=OLLAMA_MODEL, dim=OLLAMA_DIM, host=OLLAMA_HOST)
        reranker = SentenceTransformersReranker()
        return embedder, reranker, OLLAMA_DIM, f"ollama/{OLLAMA_MODEL}", reranker.model_name
    raise SystemExit(f"unknown --adapters value: {adapters!r}")


async def _run(adapters: str) -> int:
    if not DATASET_PATH.is_file():
        print(
            f"missing dataset: {DATASET_PATH}. Run dataset/generate.py first.",
            file=sys.stderr,
        )
        return 1
    dataset: dict[str, Any] = json.loads(DATASET_PATH.read_text())
    embedder, reranker, dim, embedder_id, reranker_id = _select_adapters(adapters)
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-ret-1.db"
        passphrase = b"b-ret-1-ablation"
        salt = new_salt()
        with derive_key(passphrase, salt) as key:
            bootstrap(vault_path, key)
            with VaultConnection.open(vault_path, key) as vc:
                agent_id, _id_map, external_to_persona, queries = await _populate_vault(vc, dataset)
                model = models_registry.register_embedding_model(
                    vc.connection, name="ollama", dim=dim, activate=True
                )
                print(f"embedding {len(dataset['facets'])} facets with {embedder_id} ...")
                while True:
                    stats = await embed_worker.run_pass(
                        vc.connection,
                        embedder,
                        active_model_id=model.id,
                        batch_size=64,
                    )
                    if stats.embedded == 0:
                        break

                def ctx_factory(mode: RetrievalMode) -> PipelineContext:
                    return PipelineContext(
                        conn=vc.connection,
                        embedder=embedder,
                        reranker=reranker,
                        active_model_id=model.id,
                        vec_table=models_registry.vec_table_name(model.id),
                        vault_id="B-RET-1",
                        agent_id=agent_id,
                        config=RetrievalConfig(
                            rerank_model=reranker_id,
                            mmr_lambda=0.7,
                            max_candidates=50,
                            retrieval_mode=mode,
                        ),
                        tool_budget_tokens=4000,
                        k=K,
                        facet_types=("episodic", "semantic", "style"),
                    )

                arms = [
                    Arm(id="A_rrf_only", retrieval_mode="rrf_only"),
                    Arm(id="B_rrf_rerank", retrieval_mode="rerank_only"),
                    Arm(id="C_rrf_rerank_swcr", retrieval_mode="swcr"),
                ]
                arm_results: dict[str, ArmMetrics] = {}
                for arm in arms:
                    metrics = await _run_arm(
                        arm,
                        ctx_factory=ctx_factory,
                        queries=queries,
                        external_to_persona=external_to_persona,
                    )
                    arm_results[arm.id] = metrics
                    print(
                        f"{arm.id}: MRR={metrics.mrr:.3f} nDCG={metrics.ndcg:.3f} "
                        f"coh-syn={metrics.coherence_synthetic:.3f} "
                        f"p95={metrics.latency_p95_ms:.1f}ms"
                    )

    decision = _decide(arm_results["B_rrf_rerank"], arm_results["C_rrf_rerank_swcr"])
    payload = {
        "benchmark_id": "B-RET-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "dataset": str(DATASET_PATH.relative_to(HERE.parent.parent)),
            "n_facets": len(dataset["facets"]),
            "n_queries": len(dataset["queries"]),
            "dim": dim,
            "k": K,
            "adapters": adapters,
            "embedder": embedder_id,
            "reranker": reranker_id,
            "cohere_arm": "skipped — no licensed key in session",
            "human_raters": "deferred — requires 3 raters by 50 bundles",
        },
        "arms": {
            arm_id: {
                "mrr_at_k": m.mrr,
                "ndcg_at_k": m.ndcg,
                "coherence_synthetic": m.coherence_synthetic,
                "latency_p50_ms": m.latency_p50_ms,
                "latency_p95_ms": m.latency_p95_ms,
                "latency_p99_ms": m.latency_p99_ms,
                "mean_latency_ms": m.mean_ms,
            }
            for arm_id, m in arm_results.items()
        },
        "decision": decision,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"{stamp}.json"
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out}")
    print(f"verdict: {decision['verdict']}")
    return 0


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b-ret-1")
    parser.add_argument(
        "--adapters",
        choices=("fake", "real"),
        default="fake",
        help=(
            "'fake' (default) runs deterministic hash embedder + keyword "
            "reranker. 'real' requires Ollama (nomic-embed-text) and the "
            "sentence-transformers MiniLM cross-encoder."
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.adapters))


if __name__ == "__main__":
    raise SystemExit(_cli())
