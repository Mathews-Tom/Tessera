"""B-SYNC-1 - snapshot sync load at N facets.

Builds a fresh encrypted vault, inserts synthetic project facets through the
normal capture path, pushes the snapshot through the sync primitive, restores
it to a second vault path, and verifies the restored audit chain.

The default run targets the v0.5 dogfood gate:

    uv run python docs/benchmarks/B-SYNC-1-snapshot-load/run.py --n-facets 50000

This harness intentionally defaults to LocalFilesystemStore. It measures the
shared snapshot-sync primitive and BlobStore protocol without requiring
operator-owned S3 credentials or a live network path.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tessera.migration import bootstrap
from tessera.sync.pull import pull
from tessera.sync.push import push
from tessera.sync.storage import LocalFilesystemStore
from tessera.vault import capture
from tessera.vault.audit_chain import verify_chain
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt

RESULTS_DIR = Path(__file__).parent / "results"
PASSPHRASE = b"b-sync-1-bench-passphrase"
DEFAULT_N_FACETS = 50_000


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


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = round((pct / 100.0) * (len(ordered) - 1))
    return ordered[idx]


def _ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _seed_vault(vault_path: Path, n_facets: int) -> tuple[bytes, bytes, list[float]]:
    salt = new_salt()
    save_salt(vault_path, salt)
    samples_ms: list[float] = []
    with derive_key(bytearray(PASSPHRASE), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) "
                "VALUES ('01SYNCLOAD', 'sync-load', 0)"
            )
            agent_id = int(
                vc.connection.execute(
                    "SELECT id FROM agents WHERE external_id='01SYNCLOAD'"
                ).fetchone()[0]
            )
            for i in range(n_facets):
                start = time.perf_counter()
                capture.capture(
                    vc.connection,
                    agent_id=agent_id,
                    facet_type="project",
                    content=(
                        f"sync load facet {i}: synthetic project note for "
                        "snapshot push and pull characterization"
                    ),
                    source_tool="bench",
                    captured_at=1_700_000_000 + i,
                )
                samples_ms.append(_ms_since(start))
        master_key_bytes = bytes.fromhex(key.hex())
    return salt, master_key_bytes, samples_ms


def _run(*, n_facets: int) -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_path = root / "source.db"
        restored_path = root / "restored.db"
        store_root = root / "sync-store"
        store = LocalFilesystemStore(store_root)
        store.initialize()

        populate_start = time.perf_counter()
        salt, master_key_bytes, capture_samples_ms = _seed_vault(source_path, n_facets)
        populate_ms = _ms_since(populate_start)

        push_start = time.perf_counter()
        with derive_key(bytearray(PASSPHRASE), salt) as key:
            with VaultConnection.open(source_path, key) as vc:
                push_result = push(
                    vault_path=source_path,
                    conn=vc.connection,
                    store=store,
                    master_key=master_key_bytes,
                )
        push_ms = _ms_since(push_start)

        save_salt(restored_path, salt)
        pull_start = time.perf_counter()
        pull_result = pull(
            store=store,
            target_path=restored_path,
            master_key=master_key_bytes,
        )
        pull_ms = _ms_since(pull_start)

        verify_start = time.perf_counter()
        with derive_key(bytearray(PASSPHRASE), salt) as key:
            with VaultConnection.open(restored_path, key) as vc:
                verify_result = verify_chain(vc.connection)
                restored_facets = int(
                    vc.connection.execute(
                        "SELECT count(*) FROM facets WHERE is_deleted = 0"
                    ).fetchone()[0]
                )
        verify_ms = _ms_since(verify_start)

        blob_path = store_root / "blobs" / push_result.blob_id
        source_size_bytes = source_path.stat().st_size
        blob_size_bytes = blob_path.stat().st_size
        restored_size_bytes = restored_path.stat().st_size

    total_ms = populate_ms + push_ms + pull_ms + verify_ms
    capture_metrics = {
        "p50_ms": statistics.median(capture_samples_ms),
        "p95_ms": _percentile(capture_samples_ms, 95),
        "p99_ms": _percentile(capture_samples_ms, 99),
        "mean_ms": statistics.fmean(capture_samples_ms),
        "max_ms": max(capture_samples_ms),
    }
    payload = {
        "benchmark_id": "B-SYNC-1",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": _env_block(),
        "inputs": {
            "backend": "local-filesystem",
            "facets": n_facets,
            "facet_type": "project",
        },
        "metrics": {
            "capture": capture_metrics,
            "populate_wall_ms": populate_ms,
            "push_wall_ms": push_ms,
            "pull_wall_ms": pull_ms,
            "verify_wall_ms": verify_ms,
            "total_wall_ms": total_ms,
            "source_size_bytes": source_size_bytes,
            "blob_size_bytes": blob_size_bytes,
            "restored_size_bytes": restored_size_bytes,
            "bytes_uploaded": push_result.bytes_uploaded,
            "bytes_written": pull_result.bytes_written,
            "restored_facets": restored_facets,
            "audit_rows_verified": verify_result.total_rows,
        },
        "result": {
            "push_sequence_number": push_result.sequence_number,
            "pull_sequence_number": pull_result.sequence_number,
            "audit_chain_head_matches": (
                push_result.audit_chain_head == pull_result.audit_chain_head
            ),
            "restored_facet_count_matches": restored_facets == n_facets,
        },
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
        f"facets={n_facets} push={push_ms:.1f}ms pull={pull_ms:.1f}ms "
        f"verify={verify_ms:.1f}ms restored={restored_facets} "
        f"chain_head_matches={payload['result']['audit_chain_head_matches']}"
    )
    return 0


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="b-sync-1")
    parser.add_argument("--n-facets", type=int, default=DEFAULT_N_FACETS)
    args = parser.parse_args(argv)
    if args.n_facets < 1:
        raise SystemExit("--n-facets must be >= 1")
    return _run(n_facets=args.n_facets)


if __name__ == "__main__":
    raise SystemExit(_cli())
