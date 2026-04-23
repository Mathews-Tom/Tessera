"""Deterministic seed derivation."""

from __future__ import annotations

import pytest

from tessera.retrieval.seed import (
    DEFAULT_RETRIEVAL_MODE,
    RetrievalConfig,
    compute_seed,
    seed_hex,
)


@pytest.mark.unit
def test_default_retrieval_mode_is_swcr() -> None:
    # ADR 0011: SWCR ships default-on at v0.1. Flipping this back to
    # ``rerank_only`` would contradict the post-reframe positioning and
    # must be accompanied by a new ADR.
    assert DEFAULT_RETRIEVAL_MODE == "swcr"
    assert (
        RetrievalConfig(
            rerank_model="x",
            mmr_lambda=0.7,
            max_candidates=50,
        ).retrieval_mode
        == "swcr"
    )


_CFG = RetrievalConfig(
    rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
    mmr_lambda=0.7,
    max_candidates=50,
    retrieval_mode="rrf_only",
)


@pytest.mark.unit
def test_same_inputs_produce_same_seed() -> None:
    a = compute_seed(
        query_text="hello world",
        vault_id="01V",
        active_embedding_model_id=1,
        config=_CFG,
    )
    b = compute_seed(
        query_text="hello world",
        vault_id="01V",
        active_embedding_model_id=1,
        config=_CFG,
    )
    assert a == b


@pytest.mark.unit
def test_changing_any_input_changes_the_seed() -> None:
    base = compute_seed(
        query_text="q",
        vault_id="v",
        active_embedding_model_id=1,
        config=_CFG,
    )
    different_query = compute_seed(
        query_text="q2",
        vault_id="v",
        active_embedding_model_id=1,
        config=_CFG,
    )
    different_vault = compute_seed(
        query_text="q",
        vault_id="v2",
        active_embedding_model_id=1,
        config=_CFG,
    )
    different_model = compute_seed(
        query_text="q",
        vault_id="v",
        active_embedding_model_id=2,
        config=_CFG,
    )
    different_config = compute_seed(
        query_text="q",
        vault_id="v",
        active_embedding_model_id=1,
        config=RetrievalConfig(
            rerank_model=_CFG.rerank_model,
            mmr_lambda=0.5,
            max_candidates=50,
            retrieval_mode="rrf_only",
        ),
    )
    assert len({base, different_query, different_vault, different_model, different_config}) == 5


@pytest.mark.unit
def test_seed_hex_is_16_character_lowercase_hex() -> None:
    assert seed_hex(0x1A2B3C4D5E6F7890) == "0x1a2b3c4d5e6f7890"


@pytest.mark.unit
def test_seed_fits_64_bits() -> None:
    seed = compute_seed(
        query_text="q",
        vault_id="v",
        active_embedding_model_id=1,
        config=_CFG,
    )
    assert 0 <= seed < 2**64


@pytest.mark.unit
def test_config_hash_stable_across_kwarg_order() -> None:
    # Even if future code reorders how it builds the RetrievalConfig,
    # the JSON sort_keys guarantees the hash is stable.
    c1 = RetrievalConfig(
        rerank_model="x", mmr_lambda=0.7, max_candidates=50, retrieval_mode="rrf_only"
    )
    c2 = RetrievalConfig(
        retrieval_mode="rrf_only", max_candidates=50, mmr_lambda=0.7, rerank_model="x"
    )
    assert c1.hash() == c2.hash()
