"""Reciprocal Rank Fusion — ordering, tie-break, monotonicity."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tessera.retrieval.rrf import DEFAULT_K, fuse


@dataclass
class _Ranked:
    facet_id: int
    rank: int


@pytest.mark.unit
def test_empty_lists_produce_empty_result() -> None:
    assert fuse([], []) == []


@pytest.mark.unit
def test_single_list_preserves_order() -> None:
    items = [_Ranked(1, 0), _Ranked(2, 1), _Ranked(3, 2)]
    fused = fuse(items)
    assert [r.facet_id for r in fused] == [1, 2, 3]
    # Ranks are reassigned from 0.
    assert [r.rank for r in fused] == [0, 1, 2]


@pytest.mark.unit
def test_fused_score_is_higher_when_document_appears_in_multiple_lists() -> None:
    list_a = [_Ranked(1, 0), _Ranked(2, 1)]
    list_b = [_Ranked(2, 0)]
    fused = fuse(list_a, list_b)
    by_id = {r.facet_id: r for r in fused}
    # Doc 2 appears at rank 1 in A and rank 0 in B.
    expected_doc2 = 1.0 / (DEFAULT_K + 2) + 1.0 / (DEFAULT_K + 1)
    assert by_id[2].score == pytest.approx(expected_doc2)
    assert by_id[2].score > by_id[1].score


@pytest.mark.unit
def test_ties_break_on_facet_id_ascending() -> None:
    # Same rank in separate lists — same score — tie must break by facet_id.
    list_a = [_Ranked(3, 0), _Ranked(1, 0)]
    list_b = [_Ranked(2, 0)]
    fused = fuse(list_a, list_b)
    # list_a is a single list, so both docs get the same score; list_b adds
    # doc 2 at rank 0 too. All three have identical scores.
    facet_ids = [r.facet_id for r in fused]
    scores = [r.score for r in fused]
    assert facet_ids == [1, 2, 3]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
def test_rejects_nonpositive_k() -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        fuse([_Ranked(1, 0)], k=0)
