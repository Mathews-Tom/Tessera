"""Embed worker state machine — success, retry, terminal, backoff."""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

import pytest

# Registering the Ollama embedder satisfies models_registry's adapter check
# even though the tests use a fake embedder directly against the worker.
import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.adapters.errors import (
    AdapterError,
    AdapterModelNotFoundError,
    AdapterNetworkError,
)
from tessera.retrieval import embed_worker
from tessera.retrieval.retry_policy import BACKOFF_SECONDS, MAX_ATTEMPTS
from tessera.vault import capture
from tessera.vault.connection import VaultConnection


@dataclass
class FakeEmbedder:
    name: ClassVar[str] = "fake"
    model_name: str = "fake-model"
    dim: int = 4
    sequence: list[list[float] | AdapterError] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if not self.sequence:
            raise RuntimeError("FakeEmbedder exhausted")
        outcome = self.sequence.pop(0)
        if isinstance(outcome, AdapterError):
            raise outcome
        return [outcome]

    async def health_check(self) -> None:
        return None


def _register_model(vc: VaultConnection, dim: int) -> int:
    model = models_registry.register_embedding_model(
        vc.connection, name="ollama", dim=dim, activate=True
    )
    return model.id


def _make_agent(vc: VaultConnection) -> int:
    cur = vc.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01WORKER', 'agent', 0)"
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _capture_n(vc: VaultConnection, agent_id: int, n: int) -> list[str]:
    ids: list[str] = []
    for i in range(n):
        result = capture.capture(
            vc.connection,
            agent_id=agent_id,
            facet_type="episodic",
            content=f"event-{i}",
            source_client="test",
        )
        ids.append(result.external_id)
    return ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_pass_marks_facets_embedded(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 2)
    embedder = FakeEmbedder(sequence=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])

    stats = await embed_worker.run_pass(
        open_vault.connection, embedder, active_model_id=model_id, now_epoch=100
    )

    assert stats.embedded == 2
    assert stats.retrying == 0
    assert stats.failed == 0
    statuses = open_vault.connection.execute(
        "SELECT embed_status FROM facets ORDER BY id"
    ).fetchall()
    assert [s[0] for s in statuses] == ["embedded", "embedded"]
    vec_rows = open_vault.connection.execute(f"SELECT COUNT(*) FROM vec_{model_id}").fetchone()
    assert int(vec_rows[0]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_error_flips_facet_to_failed_immediately(
    open_vault: VaultConnection,
) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 1)
    embedder = FakeEmbedder(sequence=[AdapterModelNotFoundError("no such model")])

    stats = await embed_worker.run_pass(
        open_vault.connection, embedder, active_model_id=model_id, now_epoch=100
    )

    assert stats.failed == 1
    assert stats.retrying == 0
    row = open_vault.connection.execute(
        "SELECT embed_status, embed_attempts, embed_last_error FROM facets"
    ).fetchone()
    assert row[0] == "failed"
    assert int(row[1]) == 1
    assert "AdapterModelNotFoundError" in row[2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retryable_error_stays_pending_with_incremented_attempts(
    open_vault: VaultConnection,
) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 1)
    embedder = FakeEmbedder(sequence=[AdapterNetworkError("timeout")])

    stats = await embed_worker.run_pass(
        open_vault.connection, embedder, active_model_id=model_id, now_epoch=100
    )

    assert stats.retrying == 1
    row = open_vault.connection.execute(
        "SELECT embed_status, embed_attempts, embed_last_attempt_at FROM facets"
    ).fetchone()
    assert row[0] == "pending"
    assert int(row[1]) == 1
    assert int(row[2]) == 100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_cap_flips_to_failed(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 1)
    # Pre-set attempts to MAX-1 so the next failure crosses the cap.
    open_vault.connection.execute(
        "UPDATE facets SET embed_attempts = ?, embed_last_attempt_at = 0",
        (MAX_ATTEMPTS - 1,),
    )
    embedder = FakeEmbedder(sequence=[AdapterNetworkError("still timing out")])

    stats = await embed_worker.run_pass(
        open_vault.connection,
        embedder,
        active_model_id=model_id,
        now_epoch=int(BACKOFF_SECONDS[-1]) + 1,
    )

    assert stats.failed == 1
    status = open_vault.connection.execute("SELECT embed_status FROM facets").fetchone()[0]
    assert status == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backoff_window_skips_facet(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 1)
    open_vault.connection.execute(
        "UPDATE facets SET embed_attempts = 1, embed_last_attempt_at = 100"
    )
    embedder = FakeEmbedder(sequence=[])  # no call should happen

    stats = await embed_worker.run_pass(
        open_vault.connection,
        embedder,
        active_model_id=model_id,
        now_epoch=101,  # only 1 second elapsed, first backoff is 5s
    )

    assert stats.skipped_backoff == 1
    assert stats.embedded == 0
    assert not embedder.calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vector_roundtrips_as_float32_blob(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 1)
    vec = [0.5, -0.25, 1.0, 0.125]
    embedder = FakeEmbedder(sequence=[vec])

    await embed_worker.run_pass(
        open_vault.connection, embedder, active_model_id=model_id, now_epoch=100
    )

    import json

    row = open_vault.connection.execute(
        f"SELECT vec_to_json(embedding) FROM vec_{model_id}"
    ).fetchone()
    assert row is not None
    parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert [round(v, 5) for v in parsed] == vec


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_size_caps_processed_count(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    _capture_n(open_vault, agent_id, 5)

    def make_vec(_: object) -> list[float]:
        return [0.0, 0.0, 0.0, 0.0]

    embedder = FakeEmbedder(sequence=[make_vec(None) for _ in range(5)])

    stats = await embed_worker.run_pass(
        open_vault.connection,
        embedder,
        active_model_id=model_id,
        batch_size=2,
        now_epoch=100,
    )

    assert stats.embedded == 2
    assert len(embedder.calls) == 2
    remaining = open_vault.connection.execute(
        "SELECT COUNT(*) FROM facets WHERE embed_status = 'pending'"
    ).fetchone()[0]
    assert int(remaining) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_race_with_hard_delete_during_embed_skips_vec_insert(
    open_vault: VaultConnection,
) -> None:
    """Threat-model §S7 regression: no orphan vec row if facet vanishes mid-embed."""

    agent_id = _make_agent(open_vault)
    model_id = _register_model(open_vault, dim=4)
    external_ids = _capture_n(open_vault, agent_id, 1)
    facet_external_id = external_ids[0]

    @dataclass
    class DeletingEmbedder:
        name: ClassVar[str] = "deleting"
        model_name: str = "deleting-model"
        dim: int = 4

        async def embed(self, _texts: Sequence[str]) -> list[list[float]]:
            # Simulate a concurrent hard_delete that arrives while the
            # embedder is out of process.
            from tessera.vault import facets as _facets

            _facets.hard_delete(open_vault.connection, facet_external_id)
            return [[1.0, 2.0, 3.0, 4.0]]

        async def health_check(self) -> None:
            return None

    deleting_embedder = DeletingEmbedder()

    stats = await embed_worker.run_pass(
        open_vault.connection, deleting_embedder, active_model_id=model_id, now_epoch=100
    )

    assert stats.embedded == 0
    assert stats.skipped_deleted == 1
    vec_rows = open_vault.connection.execute(f"SELECT COUNT(*) FROM vec_{model_id}").fetchone()
    assert int(vec_rows[0]) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_batch_size_rejected(open_vault: VaultConnection) -> None:
    embedder = FakeEmbedder(sequence=[])
    with pytest.raises(ValueError, match="batch_size"):
        await embed_worker.run_pass(
            open_vault.connection, embedder, active_model_id=1, batch_size=0
        )


# Compatibility anchor: if the struct packing ever changes, this test surfaces it.
@pytest.mark.unit
def test_vector_packing_is_little_endian_float32() -> None:
    from tessera.retrieval.embed_worker import _serialize_vector

    vec = [1.0, 2.0]
    packed = _serialize_vector(vec)
    assert struct.unpack("<2f", packed) == (1.0, 2.0)
    assert len(packed) == 8
