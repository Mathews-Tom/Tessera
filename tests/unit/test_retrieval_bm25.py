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
        source_tool="test",
    ).external_id


@pytest.mark.unit
def test_empty_query_returns_empty(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    _capture(open_vault, agent_id, ftype="project", content="hello world")
    assert (
        bm25.search(
            open_vault.connection,
            query_text=" ",
            agent_id=agent_id,
            facet_type="project",
        )
        == []
    )


@pytest.mark.unit
def test_search_matches_on_content_keyword(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    e1 = _capture(open_vault, agent_id, ftype="project", content="shipped retrieval P4")
    _capture(open_vault, agent_id, ftype="project", content="unrelated note about cheese")
    hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="project",
    )
    assert [h.external_id for h in hits] == [e1]


@pytest.mark.unit
def test_search_filters_by_facet_type(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    _capture(open_vault, agent_id, ftype="project", content="project retrieval notes")
    _capture(open_vault, agent_id, ftype="preference", content="preference retrieval notes")
    project_hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="project",
    )
    preference_hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="preference",
    )
    assert len(project_hits) == 1
    assert len(preference_hits) == 1
    assert project_hits[0].facet_type == "project"
    assert preference_hits[0].facet_type == "preference"


@pytest.mark.unit
def test_search_ignores_soft_deleted_rows(open_vault: VaultConnection) -> None:
    from tessera.vault import facets as _facets

    agent_id = _make_agent(open_vault)
    ext = _capture(open_vault, agent_id, ftype="project", content="shipped retrieval P4")
    _facets.soft_delete(open_vault.connection, ext)
    assert (
        bm25.search(
            open_vault.connection,
            query_text="retrieval",
            agent_id=agent_id,
            facet_type="project",
        )
        == []
    )


@pytest.mark.unit
def test_search_respects_limit(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    for i in range(5):
        _capture(open_vault, agent_id, ftype="project", content=f"shipped retrieval P4 iter {i}")
    hits = bm25.search(
        open_vault.connection,
        query_text="retrieval",
        agent_id=agent_id,
        facet_type="project",
        limit=2,
    )
    assert len(hits) == 2


@pytest.mark.unit
def test_multi_word_query_finds_non_adjacent_matches(open_vault: VaultConnection) -> None:
    """Regression: a bag-of-words query must not require adjacency.

    An earlier phrase-wrapping implementation forced all tokens adjacent
    and in order, so ``retrieval pipeline`` silently missed documents
    containing both words with other text between them.
    """

    agent_id = _make_agent(open_vault)
    _capture(
        open_vault,
        agent_id,
        ftype="project",
        content="the retrieval task for the downstream pipeline",
    )
    hits = bm25.search(
        open_vault.connection,
        query_text="retrieval pipeline",
        agent_id=agent_id,
        facet_type="project",
    )
    assert len(hits) == 1


@pytest.mark.unit
def test_search_does_not_crash_on_operator_tokens(open_vault: VaultConnection) -> None:
    # A query containing FTS5 operator tokens (AND, OR, NOT, double
    # quotes) must not raise an ``sqlite3.OperationalError`` even when it
    # matches nothing. The phrase-wrap escape keeps operators literal.
    agent_id = _make_agent(open_vault)
    _capture(open_vault, agent_id, ftype="project", content="benign content")
    # No exception is the assertion.
    bm25.search(
        open_vault.connection,
        query_text='AND OR NOT "FTS5"',
        agent_id=agent_id,
        facet_type="project",
    )


@pytest.mark.unit
def test_rejects_nonpositive_limit(open_vault: VaultConnection) -> None:
    with pytest.raises(ValueError, match="limit"):
        bm25.search(
            open_vault.connection,
            query_text="q",
            agent_id=1,
            facet_type="project",
            limit=0,
        )
