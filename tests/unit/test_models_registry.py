"""On-disk embedding-model registry: insertion, activation, vec-table shape."""

from __future__ import annotations

import pytest

# Importing the Ollama adapter module registers the "ollama" name in the
# python-side registry, which register_embedding_model cross-checks.
import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.vault.connection import VaultConnection


@pytest.mark.unit
def test_register_creates_row_and_vec_table(open_vault: VaultConnection) -> None:
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=768, activate=True
    )
    assert model.id == 1
    assert model.dim == 768
    assert model.is_active is True
    # Vec virtual table is present under the id-derived name.
    row = open_vault.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (models_registry.vec_table_name(model.id),),
    ).fetchone()
    assert row is not None


@pytest.mark.unit
def test_register_unknown_adapter_rejected(open_vault: VaultConnection) -> None:
    with pytest.raises(models_registry.ModelRegistryError):
        models_registry.register_embedding_model(
            open_vault.connection, name="not-a-registered-adapter", dim=768
        )


@pytest.mark.unit
def test_register_duplicate_rejected(open_vault: VaultConnection) -> None:
    models_registry.register_embedding_model(open_vault.connection, name="ollama", dim=768)
    with pytest.raises(models_registry.DuplicateModelError):
        models_registry.register_embedding_model(open_vault.connection, name="ollama", dim=768)


@pytest.mark.unit
def test_register_invalid_dim_rejected(open_vault: VaultConnection) -> None:
    with pytest.raises(models_registry.ModelRegistryError):
        models_registry.register_embedding_model(open_vault.connection, name="ollama", dim=0)


@pytest.mark.unit
def test_activate_flip_enforces_single_active(open_vault: VaultConnection) -> None:
    models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=768, activate=True
    )
    # Second model, different logical name but reusing the same registered adapter.
    # We simulate a second model by inserting a row with a different name that
    # does exist in the python registry: register_embedding_model refuses unless
    # the python adapter is registered under that exact name, so for this test
    # we register via a direct SQL shim — the invariant under test is the
    # unique-active partial index, not the adapter cross-check.
    open_vault.connection.execute(
        "INSERT INTO embedding_models(name, dim, added_at, is_active) VALUES (?, ?, 0, 0)",
        ("ollama-2", 768),
    )
    models_registry.ensure_vec_loaded(open_vault.connection)
    second_id = int(
        open_vault.connection.execute(
            "SELECT id FROM embedding_models WHERE name = 'ollama-2'"
        ).fetchone()[0]
    )
    open_vault.connection.execute(
        f"CREATE VIRTUAL TABLE vec_{second_id} USING vec0("
        "facet_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )

    # Flip: activating ollama-2 deactivates ollama.
    second = models_registry.activate(open_vault.connection, name="ollama-2")
    assert second.is_active is True

    active = models_registry.active_model(open_vault.connection)
    assert active.name == "ollama-2"
    count = int(
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM embedding_models WHERE is_active = 1"
        ).fetchone()[0]
    )
    assert count == 1


@pytest.mark.unit
def test_active_model_missing_raises(open_vault: VaultConnection) -> None:
    with pytest.raises(models_registry.NoActiveModelError):
        models_registry.active_model(open_vault.connection)


@pytest.mark.unit
def test_get_by_name_and_id(open_vault: VaultConnection) -> None:
    created = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=768
    )
    assert models_registry.get_by_name(open_vault.connection, "ollama").id == created.id
    assert models_registry.get_by_id(open_vault.connection, created.id).name == "ollama"
    with pytest.raises(models_registry.UnknownModelError):
        models_registry.get_by_name(open_vault.connection, "missing")
    with pytest.raises(models_registry.UnknownModelError):
        models_registry.get_by_id(open_vault.connection, 9999)


@pytest.mark.unit
def test_list_models(open_vault: VaultConnection) -> None:
    assert models_registry.list_models(open_vault.connection) == []
    models_registry.register_embedding_model(open_vault.connection, name="ollama", dim=768)
    names = [m.name for m in models_registry.list_models(open_vault.connection)]
    assert names == ["ollama"]


@pytest.mark.unit
def test_vec_table_name_rejects_invalid_id() -> None:
    with pytest.raises(models_registry.ModelRegistryError):
        models_registry.vec_table_name(0)


@pytest.mark.unit
def test_ensure_vec_loaded_is_idempotent(open_vault: VaultConnection) -> None:
    models_registry.ensure_vec_loaded(open_vault.connection)
    # Second call hits the "already loaded" branch via vec_version().
    models_registry.ensure_vec_loaded(open_vault.connection)
    version = open_vault.connection.execute("SELECT vec_version()").fetchone()[0]
    assert isinstance(version, str)
