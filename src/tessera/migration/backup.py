"""Pre-migration backup and rollback helpers per docs/migration-contract.md.

Backups are whole-file copies (never hardlinks) colocated next to the vault.
Filename carries the target version and an ISO8601 timestamp so a glob finds
the most-recent pre-migration snapshot without a catalogue table.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path


def backup_filename(vault: Path, target_version: int, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return vault.with_name(f"{vault.name}.pre-v{target_version}-{stamp}")


def make_backup(vault: Path, target_version: int, now: datetime | None = None) -> Path:
    if not vault.is_file():
        raise FileNotFoundError(f"vault file not found: {vault}")
    timestamp = now or datetime.now(UTC)
    dest = backup_filename(vault, target_version, timestamp)
    if dest.exists():
        raise FileExistsError(f"backup already exists: {dest}")
    shutil.copy2(vault, dest)
    return dest


def restore_backup(backup: Path, vault: Path, now: datetime | None = None) -> Path:
    """Swap the live vault with ``backup``.

    The current vault is renamed to ``<name>.aborted-<ts>`` so an accidental
    rollback is itself reversible; returns the aborted path. The restored
    vault lives at the original ``vault`` location.
    """

    if not backup.is_file():
        raise FileNotFoundError(f"backup not found: {backup}")
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    aborted = vault.with_name(f"{vault.name}.aborted-{timestamp}")
    if vault.exists():
        if aborted.exists():
            raise FileExistsError(f"aborted slot already in use: {aborted}")
        vault.rename(aborted)
    shutil.copy2(backup, vault)
    return aborted


def list_backups(vault: Path) -> list[Path]:
    parent = vault.parent
    prefix = f"{vault.name}.pre-v"
    return sorted(p for p in parent.iterdir() if p.name.startswith(prefix))
