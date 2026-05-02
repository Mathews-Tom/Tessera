"""End-to-end ``tessera audit verify`` exercise against a real vault.

Each test bootstraps a vault, runs through the CLI parser, and asserts
the documented exit codes per ADR 0021 §Verify CLI:

* 0 — chain intact.
* 1 — first broken row.
* 2 — schema/vault error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.cli.__main__ import main as cli_main
from tessera.migration import bootstrap
from tessera.vault import audit
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt

_PASSPHRASE = b"correct horse battery staple"


def _bootstrap_vault(path: Path) -> None:
    salt = new_salt()
    salt_path = path.with_suffix(".db.salt")
    salt_path.write_bytes(salt)
    k = derive_key(bytearray(_PASSPHRASE), salt)
    bootstrap(path, k)
    k.wipe()


@pytest.mark.integration
def test_audit_verify_returns_zero_on_intact_chain(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)

    # Append a few audit rows through the canonical insert path.
    k = derive_key(bytearray(_PASSPHRASE), tmp_path.joinpath("vault.db.salt").read_bytes())
    with VaultConnection.open(vault_path, k) as vc:
        for _ in range(3):
            audit.write(
                vc.connection,
                op="vault_opened",
                actor="cli",
                payload={"schema_version": 4},
            )
    k.wipe()

    rc = cli_main(
        [
            "audit",
            "verify",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 0


@pytest.mark.integration
def test_audit_verify_returns_one_on_broken_chain(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.db"
    _bootstrap_vault(vault_path)

    salt_path = tmp_path / "vault.db.salt"
    k = derive_key(bytearray(_PASSPHRASE), salt_path.read_bytes())
    with VaultConnection.open(vault_path, k) as vc:
        for _ in range(3):
            audit.write(
                vc.connection,
                op="vault_opened",
                actor="cli",
                payload={"schema_version": 4},
            )
        # Tamper with one row's payload to break the chain.
        target_id = int(
            vc.connection.execute(
                "SELECT id FROM audit_log WHERE op='vault_opened' LIMIT 1"
            ).fetchone()[0]
        )
        vc.connection.execute(
            "UPDATE audit_log SET payload = ? WHERE id = ?",
            ('{"schema_version": 99}', target_id),
        )
    k.wipe()

    rc = cli_main(
        [
            "audit",
            "verify",
            "--vault",
            str(vault_path),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 1


@pytest.mark.integration
def test_audit_verify_returns_two_when_vault_missing(tmp_path: Path) -> None:
    rc = cli_main(
        [
            "audit",
            "verify",
            "--vault",
            str(tmp_path / "does-not-exist.db"),
            "--passphrase",
            _PASSPHRASE.decode(),
        ]
    )
    assert rc == 2
