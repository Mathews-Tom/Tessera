"""MMR greedy selection and diversity."""

from __future__ import annotations

import pytest

from tessera.retrieval.mmr import MMRItem, diversify


@pytest.mark.unit
def test_diversify_empty_input_returns_empty() -> None:
    assert diversify([], k=5) == []


@pytest.mark.unit
def test_diversify_k_zero_returns_empty() -> None:
    items = [MMRItem(facet_id=1, relevance=0.9, embedding=[1.0, 0.0])]
    assert diversify(items, k=0) == []


@pytest.mark.unit
def test_diversify_with_lambda_one_returns_relevance_order() -> None:
    items = [
        MMRItem(facet_id=1, relevance=0.5, embedding=[1.0, 0.0]),
        MMRItem(facet_id=2, relevance=0.9, embedding=[1.0, 0.0]),
        MMRItem(facet_id=3, relevance=0.7, embedding=[0.0, 1.0]),
    ]
    result = diversify(items, k=3, mmr_lambda=1.0)
    assert [r.facet_id for r in result] == [2, 3, 1]


@pytest.mark.unit
def test_diversify_with_lambda_zero_prefers_diverse_vectors() -> None:
    items = [
        MMRItem(facet_id=1, relevance=0.9, embedding=[1.0, 0.0]),
        MMRItem(facet_id=2, relevance=0.9, embedding=[1.0, 0.0]),  # dup of 1
        MMRItem(facet_id=3, relevance=0.9, embedding=[0.0, 1.0]),  # orthogonal
    ]
    result = diversify(items, k=2, mmr_lambda=0.0)
    picked = {r.facet_id for r in result}
    assert 3 in picked  # the orthogonal item must be selected


@pytest.mark.unit
def test_diversify_ties_break_on_facet_id_ascending() -> None:
    items = [
        MMRItem(facet_id=5, relevance=0.5, embedding=[1.0, 0.0]),
        MMRItem(facet_id=2, relevance=0.5, embedding=[0.0, 1.0]),
        MMRItem(facet_id=1, relevance=0.5, embedding=[0.0, 0.0, 1.0][:2]),
    ]
    result = diversify(items, k=2, mmr_lambda=0.7)
    # First pick has no prior selections, so tie-break by facet_id ASC.
    assert result[0].facet_id == 1


@pytest.mark.unit
def test_diversify_rejects_bad_lambda() -> None:
    with pytest.raises(ValueError, match="mmr_lambda"):
        diversify(
            [MMRItem(facet_id=1, relevance=0.5, embedding=[1.0, 0.0])],
            k=1,
            mmr_lambda=1.5,
        )


@pytest.mark.unit
def test_diversify_rejects_negative_k() -> None:
    with pytest.raises(ValueError, match="k"):
        diversify([], k=-1)


@pytest.mark.unit
def test_diversify_preserves_everything_when_k_exceeds_pool() -> None:
    items = [
        MMRItem(facet_id=1, relevance=0.9, embedding=[1.0, 0.0]),
        MMRItem(facet_id=2, relevance=0.5, embedding=[0.0, 1.0]),
    ]
    result = diversify(items, k=10)
    assert len(result) == 2
