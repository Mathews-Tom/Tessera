"""SWCR algorithm — edge weights, reweighting, determinism, parameters."""

from __future__ import annotations

import pytest

from tessera.retrieval.swcr import (
    DEFAULT_PARAMS,
    SWCRCandidate,
    SWCRParams,
    apply,
)


def _cand(
    facet_id: int,
    rerank_score: float,
    embedding: list[float],
    *,
    facet_type: str = "episodic",
    entities: frozenset[str] = frozenset(),
) -> SWCRCandidate:
    return SWCRCandidate(
        facet_id=facet_id,
        rerank_score=rerank_score,
        embedding=embedding,
        facet_type=facet_type,
        entities=entities,
    )


@pytest.mark.unit
def test_empty_input_returns_empty() -> None:
    assert apply([]) == []


@pytest.mark.unit
def test_single_candidate_has_no_bonus() -> None:
    result = apply([_cand(1, 0.5, [1.0, 0.0])])
    assert len(result) == 1
    assert result[0].score == pytest.approx(0.5)
    assert result[0].rank == 0


@pytest.mark.unit
def test_coherence_bonus_adds_to_rerank_score() -> None:
    a = _cand(1, 0.5, [1.0, 0.0], facet_type="episodic", entities=frozenset({"alice"}))
    b = _cand(2, 0.5, [1.0, 0.0], facet_type="style", entities=frozenset({"alice"}))
    results = apply([a, b])
    # Both candidates should gain bonus from each other; high-similarity,
    # cross-type, entity-matching pair is the SWCR sweet spot.
    for r in results:
        assert r.score > 0.5


@pytest.mark.unit
def test_isolated_candidate_gets_smaller_bonus_than_connected_one() -> None:
    # Two clustered candidates (high similarity, shared entity, cross-type)
    # plus one orthogonal loner. The clustered pair should outrank the loner
    # even though all three start with the same rerank score.
    pair_a = _cand(1, 0.5, [1.0, 0.0, 0.0], facet_type="episodic", entities=frozenset({"x"}))
    pair_b = _cand(2, 0.5, [1.0, 0.0, 0.0], facet_type="style", entities=frozenset({"x"}))
    loner = _cand(3, 0.5, [0.0, 0.0, 1.0], facet_type="episodic", entities=frozenset({"z"}))
    results = apply([pair_a, pair_b, loner])
    scores = {r.facet_id: r.score for r in results}
    assert scores[1] > scores[3]
    assert scores[2] > scores[3]


@pytest.mark.unit
def test_cross_type_edge_is_stronger_than_same_type() -> None:
    # Same-type pair with identical embeddings and entities should still
    # get boosted, but less than a cross-type pair with the same features.
    same_a = _cand(10, 0.5, [1.0, 0.0], facet_type="episodic", entities=frozenset({"e"}))
    same_b = _cand(11, 0.5, [1.0, 0.0], facet_type="episodic", entities=frozenset({"e"}))
    cross_a = _cand(20, 0.5, [1.0, 0.0], facet_type="episodic", entities=frozenset({"e"}))
    cross_b = _cand(21, 0.5, [1.0, 0.0], facet_type="style", entities=frozenset({"e"}))
    same_scores = {r.facet_id: r.score for r in apply([same_a, same_b])}
    cross_scores = {r.facet_id: r.score for r in apply([cross_a, cross_b])}
    assert cross_scores[20] > same_scores[10]


@pytest.mark.unit
def test_tie_break_on_facet_id_ascending() -> None:
    # Identical embeddings, same type, no entities → equal edge weights,
    # identical bonuses, tie broken on facet_id ASC.
    a = _cand(7, 0.5, [1.0, 0.0])
    b = _cand(3, 0.5, [1.0, 0.0])
    results = apply([a, b])
    assert [r.facet_id for r in results] == [3, 7]


@pytest.mark.unit
def test_sparsification_threshold_drops_weak_edges() -> None:
    # Orthogonal embeddings + different types + no entities → edge weight
    # below default threshold. Applying with lam=1 would have amplified
    # any surviving edge; we check the bonus is effectively zero.
    a = _cand(1, 0.5, [1.0, 0.0, 0.0], facet_type="episodic", entities=frozenset())
    b = _cand(2, 0.5, [0.0, 1.0, 0.0], facet_type="episodic", entities=frozenset())
    results = apply([a, b], params=SWCRParams(lam=1.0))
    # The edge weight between these two = 0*0.5 + 0/(0+1)*0.3 + 0*0.2 = 0.0,
    # which is below the 0.1 threshold so bonuses stay zero.
    assert all(r.score == pytest.approx(0.5) for r in results)


@pytest.mark.unit
def test_determinism_same_input_same_output() -> None:
    candidates = [
        _cand(1, 0.9, [1.0, 0.0], facet_type="episodic", entities=frozenset({"x"})),
        _cand(2, 0.7, [0.9, 0.1], facet_type="style", entities=frozenset({"x", "y"})),
        _cand(3, 0.5, [0.0, 1.0], facet_type="semantic", entities=frozenset({"y"})),
    ]
    first = apply(candidates)
    second = apply(candidates)
    assert [(r.facet_id, r.score) for r in first] == [(r.facet_id, r.score) for r in second]


@pytest.mark.unit
def test_max_candidates_caps_the_graph() -> None:
    candidates = [_cand(i, 0.5, [float(i % 3), float((i + 1) % 3)]) for i in range(100)]
    params = SWCRParams(max_candidates=20)
    results = apply(candidates, params=params)
    assert len(results) == 20


@pytest.mark.unit
def test_rejects_out_of_range_params() -> None:
    with pytest.raises(ValueError, match="alpha"):
        SWCRParams(alpha=1.5)
    with pytest.raises(ValueError, match="beta"):
        SWCRParams(beta=-0.1)
    with pytest.raises(ValueError, match="gamma"):
        SWCRParams(gamma=0.8)
    with pytest.raises(ValueError, match="lam"):
        SWCRParams(lam=2.0)
    with pytest.raises(ValueError, match="edge_threshold"):
        SWCRParams(edge_threshold=0.5)
    with pytest.raises(ValueError, match="max_candidates"):
        SWCRParams(max_candidates=5)


@pytest.mark.unit
def test_lambda_zero_is_identity_rerank() -> None:
    candidates = [
        _cand(1, 0.9, [1.0, 0.0], entities=frozenset({"x"})),
        _cand(2, 0.5, [1.0, 0.0], entities=frozenset({"x"})),
    ]
    results = apply(candidates, params=SWCRParams(lam=0.0))
    assert {r.facet_id: r.score for r in results} == {1: 0.9, 2: 0.5}


@pytest.mark.unit
def test_default_params_match_spec() -> None:
    assert DEFAULT_PARAMS.alpha == 0.5
    assert DEFAULT_PARAMS.beta == 0.3
    assert DEFAULT_PARAMS.gamma == 0.2
    assert DEFAULT_PARAMS.lam == 0.25
    assert DEFAULT_PARAMS.edge_threshold == 0.1
    assert DEFAULT_PARAMS.max_candidates == 50
