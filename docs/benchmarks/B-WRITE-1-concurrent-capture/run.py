"""B-WRITE-1 — sustained synchronous-capture throughput.

Measures the capture-only write path (no embedding) against a fresh
encrypted vault. The P3 DoD requires capture latency p95 < 50 ms
independent of embedder state; this baseline records what the capture
path itself costs so later numbers at 10K and 100K facets have a point
of comparison.

Finalised in P12 against a multi-writer harness (``10 concurrent MCP
clients``); this first pass runs a single-writer sustained loop on the
contributor machine to establish the measurement shape.
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tessera.migration import bootstrap
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

RESULTS_DIR = Path(__file__).parent / "results"
TRIALS = 500


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


def _run() -> int:
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-write-1.db"
        passphrase = b"b-write-1-bench-passphrase"
        salt = new_salt()
        with derive_key(passphrase, salt) as key:
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
                samples_ms: list[float] = []
                import time

                # Warm the connection with one call that is discarded.
                capture.capture(
                    vc.connection,
                    agent_id=agent_id,
                    facet_type="episodic",
                    content="warmup",
                    source_client="bench",
                )
                for i in range(TRIALS):
                    start = time.perf_counter()
                    capture.capture(
                        vc.connection,
                        agent_id=agent_id,
                        facet_type="episodic",
                        content=f"bench content {i}",
                        source_client="bench",
                    )
                    samples_ms.append((time.perf_counter() - start) * 1000.0)

    metrics = {
        "p50_ms": statistics.median(samples_ms),
        "p95_ms": _percentile(samples_ms, 95),
        "p99_ms": _percentile(samples_ms, 99),
        "min_ms": min(samples_ms),
        "max_ms": max(samples_ms),
        "mean_ms": statistics.fmean(samples_ms),
        "writes_per_sec_mean": 1000.0 / statistics.fmean(samples_ms),
    }
    payload = {
        "benchmark_id": "B-WRITE-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "trials": TRIALS,
            "writer_count": 1,
            "facet_type": "episodic",
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
    raise SystemExit(_run())
