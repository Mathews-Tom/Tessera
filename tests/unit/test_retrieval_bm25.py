"""BM25 FTS5 candidate generator."""

from __future__ import annotations

import pytest

from tessera.retrieval import bm25
from tessera.vault import capture
from tessera.vault.connection import VaultConnection


def _make_agent(vc: VaultConnection) -> int:
    cur = vc.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01BM', 'a', 0)"
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _capture(vc: VaultConnection, agent_id: int, *, ftype: str, content: str) -> str:
    return capture.capture(
        vc.connection,
        agent_id=agent_id,
        facet_type=ftype,
        content=content,
        source_client="test",
    ).external_id


@pytest.mark.unit
def test_empty_query_returns_empty(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    _capture(open_vault, agent_id, ftype="episodic", content="hello world")
    assert (
        bm25.search(
            open_vault.connection,
            query_text=" ",
            agent_id=agent_id,
            facet_type="episodic",
        )
        == []
    )


@pytest.mark.unit
def test_search_matches_on_content_keyword(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    e1 = _capture(open_vault, agent_id, ftype="episodic", content="shipped retrieval P4")
    _capture(open_vault, agent_id, ftype="episodic", content="unrelated note about cheese")
    hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="episodic",
    )
    assert [h.external_id for h in hits] == [e1]


@pytest.mark.unit
def test_search_filters_by_facet_type(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    _capture(open_vault, agent_id, ftype="episodic", content="episodic retrieval P4 notes")
    _capture(open_vault, agent_id, ftype="semantic", content="semantic retrieval P4 notes")
    episodic_hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="episodic",
    )
    semantic_hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="semantic",
    )
    assert len(episodic_hits) == 1
    assert len(semantic_hits) == 1
    assert episodic_hits[0].facet_type == "episodic"
    assert semantic_hits[0].facet_type == "semantic"


@pytest.mark.unit
def test_search_ignores_soft_deleted_rows(open_vault: VaultConnection) -> None:
    from tessera.vault import facets as _facets

    agent_id = _make_agent(open_vault)
    ext = _capture(open_vault, agent_id, ftype="episodic", content="shipped retrieval P4")
    _facets.soft_delete(open_vault.connection, ext)
    assert (
        bm25.search(
            open_vault.connection,
            query_text="retrieval",
            agent_id=agent_id,
            facet_type="episodic",
        )
        == []
    )


@pytest.mark.unit
def test_search_respects_limit(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    for i in range(5):
        _capture(open_vault, agent_id, ftype="episodic", content=f"shipped retrieval P4 iter {i}")
    hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="episodic",
        limit=2,
    )
    assert len(hits) == 2


@pytest.mark.unit
def test_search_does_not_crash_on_operator_tokens(open_vault: VaultConnection) -> None:
    # A query containing FTS5 operator tokens (AND, OR, NOT, double
    # quotes) must not raise an ``sqlite3.OperationalError`` even when it
    # matches nothing. The phrase-wrap escape keeps operators literal.
    agent_id = _make_agent(open_vault)
    _capture(open_vault, agent_id, ftype="episodic", content="benign content")
    # No exception is the assertion.
    bm25.search(
        open_vault.connection,
        query_text='AND OR NOT "FTS5"',
        agent_id=agent_id,
        facet_type="episodic",
    )


@pytest.mark.unit
def test_rejects_nonpositive_limit(open_vault: VaultConnection) -> None:
    with pytest.raises(ValueError, match="limit"):
        bm25.search(
            open_vault.connection,
            query_text="q",
            agent_id=1,
            facet_type="episodic",
            limit=0,
        )
