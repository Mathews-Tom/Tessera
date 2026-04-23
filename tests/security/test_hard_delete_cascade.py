"""Hard delete removes every row and vector trace of a facet.

docs/threat-model.md §S7 requires that ``tessera vault purge`` (the P8 MCP
entry point) leave no residue in any vec table, no residue in the FTS
index, and no residue in the facets row itself. This test verifies the
cascade the P3 ``hard_delete`` implementation performs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

import pytest

import tessera.adapters.ollama_embedder  # noqa: F401 — adapter registration side effect
from tessera.adapters import models_registry
from tessera.retrieval import embed_worker
from tessera.vault import capture, facets
from tessera.vault.connection import VaultConnection


class _FixedEmbedder:
    name: ClassVar[str] = "fake"
    model_name: str = "fake"
    dim: int = 4

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def health_check(self) -> None:
        return None


@pytest.mark.security
@pytest.mark.asyncio
async def test_hard_delete_removes_facet_fts_and_vec(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    cur = conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01PURGE', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    model_a = models_registry.register_embedding_model(conn, name="ollama", dim=4, activate=True)
    result = capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="secret thing the agent remembered",
        source_tool="test",
    )
    await embed_worker.run_pass(conn, _FixedEmbedder(), active_model_id=model_a.id, now_epoch=100)

    # FTS has the row.
    fts_before = conn.execute(
        "SELECT COUNT(*) FROM facets_fts WHERE content MATCH 'secret'"
    ).fetchone()[0]
    assert int(fts_before) == 1
    # Vec table has the row.
    vec_before = conn.execute(f"SELECT COUNT(*) FROM vec_{model_a.id}").fetchone()[0]
    assert int(vec_before) == 1

    deleted = facets.hard_delete(conn, result.external_id)
    assert deleted is True

    assert (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM facets WHERE external_id=?", (result.external_id,)
            ).fetchone()[0]
        )
        == 0
    )
    assert (
        int(
            conn.execute("SELECT COUNT(*) FROM facets_fts WHERE content MATCH 'secret'").fetchone()[
                0
            ]
        )
        == 0
    )
    assert int(conn.execute(f"SELECT COUNT(*) FROM vec_{model_a.id}").fetchone()[0]) == 0


@pytest.mark.security
@pytest.mark.asyncio
async def test_hard_delete_cascades_across_multiple_vec_tables(
    open_vault: VaultConnection,
) -> None:
    conn = open_vault.connection
    cur = conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01MULTI', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    model_a = models_registry.register_embedding_model(conn, name="ollama", dim=4, activate=True)
    result = capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="content A",
        source_tool="test",
    )
    await embed_worker.run_pass(conn, _FixedEmbedder(), active_model_id=model_a.id, now_epoch=100)

    # Register a second model and embed under it too (simulates a shadow /
    # in-progress embedder swap per ADR 0003).
    models_registry.ensure_vec_loaded(conn)
    conn.execute(
        "INSERT INTO embedding_models(name, dim, added_at, is_active) VALUES ('ollama-2', 4, 0, 0)"
    )
    second_id = int(
        conn.execute("SELECT id FROM embedding_models WHERE name='ollama-2'").fetchone()[0]
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_{second_id} USING vec0("
        "facet_id INTEGER PRIMARY KEY, embedding FLOAT[4])"
    )
    import struct

    blob = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
    facet_id = int(
        conn.execute("SELECT id FROM facets WHERE external_id=?", (result.external_id,)).fetchone()[
            0
        ]
    )
    conn.execute(
        f"INSERT INTO vec_{second_id}(facet_id, embedding) VALUES (?, ?)",
        (facet_id, blob),
    )

    assert int(conn.execute(f"SELECT COUNT(*) FROM vec_{second_id}").fetchone()[0]) == 1

    facets.hard_delete(conn, result.external_id)

    assert int(conn.execute(f"SELECT COUNT(*) FROM vec_{model_a.id}").fetchone()[0]) == 0
    assert int(conn.execute(f"SELECT COUNT(*) FROM vec_{second_id}").fetchone()[0]) == 0


@pytest.mark.security
def test_hard_delete_missing_external_id_returns_false(open_vault: VaultConnection) -> None:
    assert facets.hard_delete(open_vault.connection, "01MISSING") is False
