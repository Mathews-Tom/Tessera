"""B-RET-1 graph-backing side experiment.

Compares current SWCR entity-Jaccard β-term with an experiment-only sqlite
recursive-CTE typed entity graph β-term on the S1′ person/skill dataset.
Production retrieval code is deliberately not imported or modified.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

HERE = Path(__file__).parent
DATASET_PATH = HERE.parent / "B-RET-1-swcr-ablation" / "dataset" / "s1_prime.json"
RESULTS_DIR = HERE / "results"
FAKE_DIM = 16
K = 5
MAX_CANDIDATES = 50
ALPHA = 0.5
BETA = 0.3
GAMMA = 0.2
LAMBDA = 0.25
EDGE_THRESHOLD = 0.1
JACCARD_EPSILON = 1.0
Variant = Literal["baseline_jaccard", "sqlite_cte_typed_entity"]


@dataclass(frozen=True, slots=True)
class Facet:
    facet_id: int
    persona: str
    facet_type: str
    content: str
    entities: frozenset[str]
    people: tuple[str, ...]
    skill_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Query:
    query_text: str
    persona: str
    relevant_facet_ids: frozenset[int]
    query_class: str


@dataclass(frozen=True, slots=True)
class Candidate:
    facet: Facet
    rerank_score: float
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Metrics:
    mrr: float
    ndcg: float
    persona_purity: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    mean_ms: float


def _load_dataset(path: Path) -> tuple[list[Facet], list[Query], dict[str, Any]]:
    data = json.loads(path.read_text())
    facets = [
        Facet(
            facet_id=int(raw["facet_id"]),
            persona=str(raw["persona"]),
            facet_type=str(raw["facet_type"]),
            content=str(raw["content"]),
            entities=frozenset(str(e) for e in raw.get("entities", [])),
            people=tuple(str(p) for p in raw.get("people", [])),
            skill_names=tuple(str(s) for s in raw.get("skill_names", [])),
        )
        for raw in data["facets"]
    ]
    queries = [
        Query(
            query_text=str(raw["query_text"]),
            persona=str(raw["persona"]),
            relevant_facet_ids=frozenset(int(fid) for fid in raw["relevant_facet_ids"]),
            query_class=str(raw.get("query_class", "persona_recall")),
        )
        for raw in data["queries"]
    ]
    return facets, queries, data


def _hash_embedding(text: str) -> tuple[float, ...]:
    digest = hashlib.sha256(text.encode()).digest()
    return tuple(((digest[i % 32] / 255.0) * 2.0) - 1.0 for i in range(FAKE_DIM))


def _keyword_score(query: str, passage: str) -> float:
    tokens = {t.lower() for t in query.split()}
    body = passage.lower()
    overlap = sum(1 for token in tokens if token in body)
    return overlap / (1 + len(passage.split()))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a)
    norm_b = sum(y * y for y in b)
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom == 0.0:
        return 0.0
    return dot / denom


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / (len(a | b) + JACCARD_EPSILON)


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = round((pct / 100.0) * (len(ordered) - 1))
    return ordered[idx]


def _mrr_at_k(result_ids: list[int], relevant_ids: frozenset[int]) -> float:
    for idx, facet_id in enumerate(result_ids[:K]):
        if facet_id in relevant_ids:
            return 1.0 / (idx + 1)
    return 0.0


def _ndcg_at_k(result_ids: list[int], relevant_ids: frozenset[int]) -> float:
    gains = [1.0 if facet_id in relevant_ids else 0.0 for facet_id in result_ids[:K]]
    dcg = sum(gain / math.log2(idx + 2) for idx, gain in enumerate(gains))
    ideal = [1.0] * min(K, len(relevant_ids))
    idcg = sum(gain / math.log2(idx + 2) for idx, gain in enumerate(ideal))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def _persona_purity_at_k(result_ids: list[int], facets_by_id: dict[int, Facet], persona: str) -> float:
    top = result_ids[:K]
    if not top:
        return 0.0
    return sum(1 for facet_id in top if facets_by_id[facet_id].persona == persona) / len(top)


def _build_graph(facets: Sequence[Facet]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE graph_edge(src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY(src, dst))")
    for facet in facets:
        facet_node = f"facet:{facet.facet_id}"
        linked_nodes = [
            *(f"person:{name}" for name in facet.people),
            *(f"skill:{name}" for name in facet.skill_names),
            *(f"entity:{name}" for name in facet.entities),
        ]
        for node in linked_nodes:
            conn.execute("INSERT OR IGNORE INTO graph_edge(src, dst) VALUES (?, ?)", (facet_node, node))
            conn.execute("INSERT OR IGNORE INTO graph_edge(src, dst) VALUES (?, ?)", (node, facet_node))
    conn.execute("CREATE INDEX graph_edge_src ON graph_edge(src)")
    return conn


def _cte_neighborhoods(conn: sqlite3.Connection) -> dict[int, frozenset[str]]:
    rows = conn.execute(
        """
        WITH RECURSIVE walk(root, node, depth) AS (
            SELECT src AS root, dst AS node, 1 AS depth
            FROM graph_edge
            WHERE src LIKE 'facet:%'
            UNION
            SELECT walk.root, graph_edge.dst, walk.depth + 1
            FROM walk
            JOIN graph_edge ON graph_edge.src = walk.node
            WHERE walk.depth < 2
        )
        SELECT root, node FROM walk WHERE root != node
        ORDER BY root, node
        """
    ).fetchall()
    neighborhoods: dict[int, set[str]] = {}
    for root, node in rows:
        facet_id = int(str(root).split(":", 1)[1])
        neighborhoods.setdefault(facet_id, set()).add(str(node))
    return {facet_id: frozenset(nodes) for facet_id, nodes in neighborhoods.items()}


def _rank(
    query: Query,
    facets: Sequence[Facet],
    *,
    beta_source: Callable[[Facet, Facet], float],
) -> list[int]:
    query_embedding = _hash_embedding(query.query_text)
    candidates = [
        Candidate(
            facet=facet,
            rerank_score=_keyword_score(query.query_text, facet.content),
            embedding=_hash_embedding(facet.content),
        )
        for facet in facets
    ]
    candidates.sort(
        key=lambda candidate: (
            -candidate.rerank_score,
            -_cosine(query_embedding, candidate.embedding),
            candidate.facet.facet_id,
        )
    )
    top = candidates[:MAX_CANDIDATES]
    rescored: list[tuple[int, float]] = []
    for candidate in top:
        bonus_total = 0.0
        for other in top:
            if candidate.facet.facet_id == other.facet.facet_id:
                continue
            semantic = _cosine(candidate.embedding, other.embedding)
            beta_value = beta_source(candidate.facet, other.facet)
            cross_type = 1.0 if candidate.facet.facet_type != other.facet.facet_type else 0.0
            edge_weight = ALPHA * semantic + BETA * beta_value + GAMMA * cross_type
            if edge_weight < EDGE_THRESHOLD:
                continue
            bonus_total += edge_weight * other.rerank_score
        score = candidate.rerank_score + (LAMBDA * bonus_total)
        rescored.append((candidate.facet.facet_id, score))
    rescored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [facet_id for facet_id, _ in rescored]


def _run_variant(variant: Variant, facets: list[Facet], queries: list[Query]) -> Metrics:
    facets_by_id = {facet.facet_id: facet for facet in facets}
    neighborhoods: dict[int, frozenset[str]] = {}
    if variant == "sqlite_cte_typed_entity":
        conn = _build_graph(facets)
        try:
            neighborhoods = _cte_neighborhoods(conn)
        finally:
            conn.close()

    def beta_source(a: Facet, b: Facet) -> float:
        if variant == "baseline_jaccard":
            return _jaccard(a.entities, b.entities)
        return _jaccard(
            neighborhoods.get(a.facet_id, frozenset()),
            neighborhoods.get(b.facet_id, frozenset()),
        )

    mrrs: list[float] = []
    ndcgs: list[float] = []
    purities: list[float] = []
    latencies: list[float] = []
    for query in queries:
        start = time.perf_counter()
        ranked = _rank(query, facets, beta_source=beta_source)
        latencies.append((time.perf_counter() - start) * 1000.0)
        mrrs.append(_mrr_at_k(ranked, query.relevant_facet_ids))
        ndcgs.append(_ndcg_at_k(ranked, query.relevant_facet_ids))
        purities.append(_persona_purity_at_k(ranked, facets_by_id, query.persona))
    return Metrics(
        mrr=statistics.fmean(mrrs),
        ndcg=statistics.fmean(ndcgs),
        persona_purity=statistics.fmean(purities),
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        latency_p99_ms=_percentile(latencies, 99),
        mean_ms=statistics.fmean(latencies),
    )


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "unknown"
    return out.decode().strip()


def _metric_block(metrics: Metrics) -> dict[str, float]:
    return {
        "mrr_at_5": metrics.mrr,
        "ndcg_at_5": metrics.ndcg,
        "persona_purity_at_5": metrics.persona_purity,
        "latency_p50_ms": metrics.latency_p50_ms,
        "latency_p95_ms": metrics.latency_p95_ms,
        "latency_p99_ms": metrics.latency_p99_ms,
        "mean_latency_ms": metrics.mean_ms,
    }


def _main() -> int:
    if not DATASET_PATH.is_file():
        print(f"missing dataset: {DATASET_PATH}", file=sys.stderr)
        return 1
    facets, queries, raw = _load_dataset(DATASET_PATH)
    results = {
        variant: _run_variant(variant, facets, queries)
        for variant in ("baseline_jaccard", "sqlite_cte_typed_entity")
    }
    baseline = results["baseline_jaccard"]
    cte = results["sqlite_cte_typed_entity"]
    payload = {
        "benchmark_id": "B-RET-1-graph-backing-experiment",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": {
            "os": platform.platform(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "tessera_sha": _git_sha(),
        },
        "inputs": {
            "dataset": "docs/benchmarks/B-RET-1-swcr-ablation/dataset/s1_prime.json",
            "dataset_variant": raw.get("dataset_variant", "unknown"),
            "n_facets": len(facets),
            "n_people": len(raw.get("people", [])),
            "n_queries": len(queries),
            "query_classes": sorted({query.query_class for query in queries}),
            "graph_model": "typed_entity_graph",
            "cte_depth": 2,
            "adapters": "deterministic fake hash embedder + keyword reranker proxy",
            "production_code_changed": False,
        },
        "variants": {variant: _metric_block(metrics) for variant, metrics in results.items()},
        "delta_cte_minus_baseline": {
            "mrr_at_5": cte.mrr - baseline.mrr,
            "ndcg_at_5": cte.ndcg - baseline.ndcg,
            "persona_purity_at_5": cte.persona_purity - baseline.persona_purity,
            "latency_p95_ms": cte.latency_p95_ms - baseline.latency_p95_ms,
        },
        "interpretation": (
            "SQLite recursive-CTE typed entity neighborhoods did not materially improve "
            "automated S1 prime ranking metrics over the current entity-Jaccard beta-term "
            "in this offline fake-adapter proxy. Treat as decision input, not a production metric."
        ),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    for variant, metrics in results.items():
        print(
            f"{variant}: MRR={metrics.mrr:.3f} nDCG={metrics.ndcg:.3f} "
            f"purity={metrics.persona_purity:.3f} p95={metrics.latency_p95_ms:.1f}ms"
        )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
