"""Token-budget enforcement and snippet truncation."""

from __future__ import annotations

import pytest

from tessera.retrieval.budget import (
    BudgetedItem,
    apply_budget,
    count_tokens,
    truncate_snippet,
)


@pytest.mark.unit
def test_count_tokens_returns_nonzero_for_nonempty_text() -> None:
    assert count_tokens("hello world") >= 2
    assert count_tokens("") == 0


@pytest.mark.unit
def test_truncate_snippet_returns_text_unchanged_when_under_cap() -> None:
    text = "short snippet"
    assert truncate_snippet(text, max_tokens=512) == text


@pytest.mark.unit
def test_truncate_snippet_cuts_at_token_boundary() -> None:
    text = "one two three four five six seven eight nine ten"
    truncated = truncate_snippet(text, max_tokens=3)
    assert count_tokens(truncated) <= 3
    assert truncated != text


@pytest.mark.unit
def test_truncate_snippet_rejects_nonpositive_cap() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        truncate_snippet("x", max_tokens=0)


@pytest.mark.unit
def test_apply_budget_keeps_items_until_exhausted() -> None:
    items = [BudgetedItem(key=str(i), snippet=f"s{i}", token_count=10) for i in range(5)]
    result = apply_budget(items, total_budget=25)
    assert len(result.items) == 2
    assert result.truncated is True


@pytest.mark.unit
def test_apply_budget_fits_all_when_under_cap() -> None:
    items = [BudgetedItem(key="a", snippet="x", token_count=5)]
    result = apply_budget(items, total_budget=100)
    assert len(result.items) == 1
    assert result.truncated is False


@pytest.mark.unit
def test_apply_budget_with_zero_items_not_truncated() -> None:
    result = apply_budget([], total_budget=100)
    assert result.items == ()
    assert result.truncated is False


@pytest.mark.unit
def test_apply_budget_rejects_nonpositive_budget() -> None:
    with pytest.raises(ValueError, match="total_budget"):
        apply_budget([], total_budget=0)
