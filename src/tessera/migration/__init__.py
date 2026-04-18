"""Vault schema migrations per docs/migration-contract.md."""

from tessera.migration.backup import list_backups, make_backup, restore_backup
from tessera.migration.runner import (
    MigrationError,
    MigrationStep,
    UnknownTargetError,
    VaultAlreadyInitializedError,
    bootstrap,
    resume_interrupted,
    upgrade,
)

__all__ = [
    "MigrationError",
    "MigrationStep",
    "UnknownTargetError",
    "VaultAlreadyInitializedError",
    "bootstrap",
    "list_backups",
    "make_backup",
    "restore_backup",
    "resume_interrupted",
    "upgrade",
]
