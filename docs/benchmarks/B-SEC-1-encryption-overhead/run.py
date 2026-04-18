"""B-SEC-1: encryption-at-rest overhead baseline against a 1K-facet vault.

Writes a new timestamped JSON under ``results/`` per the benchmark contract
(docs/benchmarks/README.md). Compares sqlcipher-encrypted vault performance
against a plain sqlite3 baseline on the same schema for:

* **unlock** — wall clock from ``connect()`` through ``PRAGMA key`` to the
  first successful ``SELECT``.
* **write** — single-facet insert latency (p50 / p95) across 500 trials.
* **read**  — single-facet lookup by ``external_id`` (p50 / p95) across 500
  trials over a populated 1K-facet vault.

The full retrieval-ratio measurement called out in the v0.1 DoD is scoped
for P12 after the retrieval pipeline lands; at P1 this harness establishes
the measurement discipline and the first baseline number.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlcipher3
from ulid import ULID

from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt
from tessera.vault.schema import all_statements

BENCHMARK_ID = "B-SEC-1"
FACETS = 1000
TRIALS = 500
PASSPHRASE = b"b-sec-1 baseline harness"


def main() -> int:
    bench_dir = Path(__file__).parent
    enc_fd, encrypted_path = tempfile.mkstemp(suffix=".encrypted.db")
    plain_fd, plain_path = tempfile.mkstemp(suffix=".plain.db")
    import os as _os  # local alias to avoid unused top-level import

    _os.close(enc_fd)
    _os.close(plain_fd)
    encrypted_vault = Path(encrypted_path)
    plain_vault = Path(plain_path)
    # Remove the empty files so sqlcipher/sqlite3 can create fresh ones; the
    # cleanup block below re-deletes if we wrote something.
    encrypted_vault.unlink(missing_ok=True)
    plain_vault.unlink(missing_ok=True)

    try:
        enc_metrics = _measure_encrypted(encrypted_vault)
        plain_metrics = _measure_plain(plain_vault)
    finally:
        encrypted_vault.unlink(missing_ok=True)
        plain_vault.unlink(missing_ok=True)

    result = {
        "benchmark_id": BENCHMARK_ID,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "env": _environment(),
        "inputs": {"facets": FACETS, "trials": TRIALS, "passphrase_bytes": len(PASSPHRASE)},
        "metrics": {
            "encrypted": enc_metrics,
            "plain": plain_metrics,
            "overhead": _overhead(enc_metrics, plain_metrics),
        },
    }

    results_dir = bench_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = results_dir / f"{stamp}.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))
    return 0


def _measure_encrypted(path: Path) -> dict[str, Any]:
    salt = new_salt()
    k = derive_key(bytearray(PASSPHRASE), salt)
    t0 = time.perf_counter()
    bootstrap(path, k)
    bootstrap_ms = (time.perf_counter() - t0) * 1000.0
    k.wipe()

    unlocks: list[float] = []
    for _ in range(20):
        k2 = derive_key(bytearray(PASSPHRASE), salt)
        t0 = time.perf_counter()
        with VaultConnection.open(path, k2) as vc:
            vc.connection.execute("SELECT 1").fetchone()
        unlocks.append((time.perf_counter() - t0) * 1000.0)
        k2.wipe()

    k3 = derive_key(bytearray(PASSPHRASE), salt)
    with VaultConnection.open(path, k3) as vc:
        conn = vc.connection
        conn.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
            ("01BENCHAGENT", "bench", 1),
        )
        agent_id = conn.execute("SELECT id FROM agents").fetchone()[0]
        ids = _populate(conn, agent_id, FACETS)
        write_ms, read_ms = _timed_crud(conn, agent_id, ids, TRIALS)
    k3.wipe()

    return {
        "bootstrap_ms": bootstrap_ms,
        "unlock_p50_ms": statistics.median(unlocks),
        "unlock_p95_ms": _percentile(unlocks, 95),
        "write_p50_ms": statistics.median(write_ms),
        "write_p95_ms": _percentile(write_ms, 95),
        "read_p50_ms": statistics.median(read_ms),
        "read_p95_ms": _percentile(read_ms, 95),
    }


def _measure_plain(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(path), isolation_level=None)
    for stmt in all_statements():
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
        ("01BENCHAGENT", "bench", 1),
    )
    agent_id = conn.execute("SELECT id FROM agents").fetchone()[0]
    ids = _populate(conn, agent_id, FACETS)
    write_ms, read_ms = _timed_crud(conn, agent_id, ids, TRIALS)
    conn.close()

    return {
        "write_p50_ms": statistics.median(write_ms),
        "write_p95_ms": _percentile(write_ms, 95),
        "read_p50_ms": statistics.median(read_ms),
        "read_p95_ms": _percentile(read_ms, 95),
    }


def _populate(
    conn: sqlite3.Connection | sqlcipher3.Connection, agent_id: int, count: int
) -> list[str]:
    ids: list[str] = []
    for i in range(count):
        external_id = str(ULID())
        ids.append(external_id)
        conn.execute(
            """
            INSERT INTO facets(
                external_id, agent_id, facet_type, content, content_hash,
                source_client, captured_at
            ) VALUES (?, ?, 'semantic', ?, ?, 'bench', ?)
            """,
            (external_id, agent_id, f"content-{i}", f"hash-{i}", i),
        )
    return ids


def _timed_crud(
    conn: sqlite3.Connection | sqlcipher3.Connection,
    agent_id: int,
    ids: list[str],
    trials: int,
) -> tuple[list[float], list[float]]:
    write_samples: list[float] = []
    for i in range(trials):
        external_id = str(ULID())
        t0 = time.perf_counter()
        conn.execute(
            """
            INSERT INTO facets(
                external_id, agent_id, facet_type, content, content_hash,
                source_client, captured_at
            ) VALUES (?, ?, 'semantic', ?, ?, 'bench-trial', ?)
            """,
            (external_id, agent_id, f"timed-{i}", f"th-{i}", i + 10_000),
        )
        write_samples.append((time.perf_counter() - t0) * 1000.0)

    read_samples: list[float] = []
    for external_id in ids[:trials]:
        t0 = time.perf_counter()
        conn.execute(
            "SELECT id, content FROM facets WHERE external_id = ?", (external_id,)
        ).fetchone()
        read_samples.append((time.perf_counter() - t0) * 1000.0)
    return write_samples, read_samples


def _overhead(encrypted: dict[str, Any], plain: dict[str, Any]) -> dict[str, float]:
    def ratio(enc_key: str, plain_key: str) -> float:
        return (
            float(encrypted[enc_key]) / float(plain[plain_key])
            if plain[plain_key]
            else float("inf")
        )

    return {
        "write_p50_ratio": ratio("write_p50_ms", "write_p50_ms"),
        "write_p95_ratio": ratio("write_p95_ms", "write_p95_ms"),
        "read_p50_ratio": ratio("read_p50_ms", "read_p50_ms"),
        "read_p95_ratio": ratio("read_p95_ms", "read_p95_ms"),
    }


def _percentile(samples: list[float], p: int) -> float:
    if not samples:
        return 0.0
    data = sorted(samples)
    k = max(0, min(len(data) - 1, round((p / 100.0) * (len(data) - 1))))
    return data[k]


def _environment() -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "arch": platform.machine(),
        "tessera_sha": _git_sha(),
        "sqlcipher_version": getattr(sqlcipher3, "sqlite_version", "unknown"),
        "sqlite_version": sqlite3.sqlite_version,
    }


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
