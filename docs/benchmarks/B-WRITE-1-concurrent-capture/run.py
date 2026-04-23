"""B-WRITE-1 — concurrent-capture throughput against a preloaded vault.

Measures the capture-only write path (no embedding) on a vault that
already holds ``--preload`` facets, with ``--writers`` concurrent
threads each opening its own sqlcipher connection and issuing
``--trials`` captures. The aggregate run records per-write latency
and end-to-end throughput so the DoD can pin "p99 < 200 ms at ≥ 50
writes/sec" on the reference hardware.

Concurrency model: one sqlcipher connection per writer. sqlite's
WAL mode plus argon2id's per-connection KDF cost makes unlocking
expensive at open time, so we amortise by issuing every trial
against the same long-lived connection inside each thread. The
outer process blocks until every thread completes; each thread's
per-call timings are merged into one sample set for the summary
metrics.

Legacy single-writer harness was deleted with the reframe (it
measured ``episodic`` content under the pre-ADR-0010 vocabulary);
this harness is the v0.1 DoD finalisation.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tessera.migration import bootstrap
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, load_salt, new_salt, save_salt

RESULTS_DIR = Path(__file__).parent / "results"
PASSPHRASE = b"b-write-1-bench-passphrase"
DEFAULT_PRELOAD = 10_000
DEFAULT_WRITERS = 10
DEFAULT_TRIALS = 100  # per-writer


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


def _preload(vault_path: Path, agent_id: int, n: int) -> None:
    """Fill the vault with ``n`` facets before the concurrent loop starts.

    Preload happens through a single dedicated connection so the
    captures run as fast as possible; the concurrent loop then runs
    against a realistic vault size. Each capture writes a distinct
    ``content`` so the dedup path does not short-circuit the writes.
    """

    salt = load_salt(vault_path)
    with (
        derive_key(bytearray(PASSPHRASE), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        for i in range(n):
            capture.capture(
                vc.connection,
                agent_id=agent_id,
                facet_type="project",
                content=f"preload row {i} — warm the vault to a realistic size",
                source_tool="bench",
            )


def _writer_loop(
    vault_path: Path,
    agent_id: int,
    writer_idx: int,
    trials: int,
    samples_bucket: list[float],
    lock: threading.Lock,
    start_barrier: threading.Barrier,
) -> None:
    salt = load_salt(vault_path)
    with (
        derive_key(bytearray(PASSPHRASE), salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        # Every writer waits on the barrier so the concurrent window
        # is tight — otherwise faster-booting writers would finish
        # before slower ones entered the hot loop.
        start_barrier.wait()
        local: list[float] = []
        for i in range(trials):
            start = time.perf_counter()
            capture.capture(
                vc.connection,
                agent_id=agent_id,
                facet_type="project",
                content=f"writer {writer_idx} trial {i} concurrent capture",
                source_tool="bench",
            )
            local.append((time.perf_counter() - start) * 1000.0)
    with lock:
        samples_bucket.extend(local)


def _run(*, preload: int, writers: int, trials: int) -> int:
    with TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "b-write-1.db"
        salt = new_salt()
        save_salt(vault_path, salt)
        with derive_key(bytearray(PASSPHRASE), salt) as key:
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

        if preload > 0:
            _preload(vault_path, agent_id, preload)

        samples_ms: list[float] = []
        lock = threading.Lock()
        start_barrier = threading.Barrier(writers)
        threads = [
            threading.Thread(
                target=_writer_loop,
                args=(vault_path, agent_id, idx, trials, samples_ms, lock, start_barrier),
                name=f"b-write-1-writer-{idx}",
                daemon=True,
            )
            for idx in range(writers)
        ]
        wall_start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall_ms = (time.perf_counter() - wall_start) * 1000.0

    total_writes = writers * trials
    metrics = {
        "p50_ms": statistics.median(samples_ms),
        "p95_ms": _percentile(samples_ms, 95),
        "p99_ms": _percentile(samples_ms, 99),
        "min_ms": min(samples_ms),
        "max_ms": max(samples_ms),
        "mean_ms": statistics.fmean(samples_ms),
        "wall_ms": wall_ms,
        "writes_per_sec_aggregate": total_writes * 1000.0 / wall_ms if wall_ms > 0 else 0.0,
    }
    payload = {
        "benchmark_id": "B-WRITE-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "preload": preload,
            "writers": writers,
            "trials_per_writer": trials,
            "total_writes": total_writes,
            "facet_type": "project",
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
        f"aggregate={metrics['writes_per_sec_aggregate']:.1f} writes/sec "
        f"(DoD target: >=50 writes/sec, p99<200ms)"
    )
    return 0


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b-write-1")
    parser.add_argument("--preload", type=int, default=DEFAULT_PRELOAD)
    parser.add_argument("--writers", type=int, default=DEFAULT_WRITERS)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    args = parser.parse_args(argv)
    return _run(preload=args.preload, writers=args.writers, trials=args.trials)


if __name__ == "__main__":
    raise SystemExit(_cli())
