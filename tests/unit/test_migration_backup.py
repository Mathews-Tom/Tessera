"""make_backup, restore_backup, and list_backups behaviors."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera.migration import backup


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    p = tmp_path / "vault.db"
    p.write_bytes(b"tessera payload v1")
    return p


@pytest.mark.unit
def test_backup_filename_encodes_target_and_timestamp(tmp_path: Path) -> None:
    when = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    name = backup.backup_filename(tmp_path / "vault.db", 3, when)
    assert name.name == "vault.db.pre-v3-20260419T120000Z"


@pytest.mark.unit
def test_make_backup_copies_bytes_verbatim(vault: Path) -> None:
    when = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    dest = backup.make_backup(vault, target_version=2, now=when)
    assert dest.read_bytes() == vault.read_bytes()


@pytest.mark.unit
def test_make_backup_refuses_overwrite(vault: Path) -> None:
    when = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    backup.make_backup(vault, target_version=2, now=when)
    with pytest.raises(FileExistsError):
        backup.make_backup(vault, target_version=2, now=when)


@pytest.mark.unit
def test_make_backup_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup.make_backup(tmp_path / "absent.db", target_version=2)


@pytest.mark.unit
def test_restore_backup_swaps_current_aside(vault: Path, tmp_path: Path) -> None:
    when = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    snap = backup.make_backup(vault, target_version=2, now=when)
    vault.write_bytes(b"post-migration bytes")
    aborted = backup.restore_backup(snap, vault, now=when)
    assert vault.read_bytes() == b"tessera payload v1"
    assert aborted.read_bytes() == b"post-migration bytes"
    assert aborted.parent == tmp_path


@pytest.mark.unit
def test_restore_backup_missing_snapshot_raises(vault: Path, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup.restore_backup(tmp_path / "absent.db", vault)


@pytest.mark.unit
def test_list_backups_finds_pre_migration_snapshots(vault: Path) -> None:
    first = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    second = datetime(2026, 4, 19, 13, 0, 0, tzinfo=UTC)
    a = backup.make_backup(vault, target_version=2, now=first)
    b = backup.make_backup(vault, target_version=3, now=second)
    listed = backup.list_backups(vault)
    assert [p.name for p in listed] == [a.name, b.name]
