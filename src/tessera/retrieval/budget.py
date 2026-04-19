"""Token-budget enforcement for retrieval responses.

``docs/system-design.md §Retrieval pipeline`` hard-rule 5 requires every
tool's response to fit its declared token budget, measured with
``tiktoken cl100k_base``. Snippets are truncated to 256 tokens each; the
response as a whole is trimmed to the budget, dropping trailing
candidates rather than silently returning oversized output.

Tokenizer choice is load-bearing and imperfect. ``cl100k_base`` is the
OpenAI GPT-3.5/4 tokenizer; an agent running Llama or Qwen will tokenize
content under a different BPE. This is documented as a known gap in
``docs/release-spec.md §v0.1 DoD`` and accepted for v0.1 because
reproducible budgets across agent harnesses matter more than exact
fidelity to any one model's tokenizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import tiktoken

TOKENIZER_NAME: Final[str] = "cl100k_base"
SNIPPET_MAX_TOKENS: Final[int] = 256

_encoder: tiktoken.Encoding | None = None


def _encoder_singleton() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding(TOKENIZER_NAME)
    return _encoder


def count_tokens(text: str) -> int:
    return len(_encoder_singleton().encode(text))


def truncate_snippet(text: str, *, max_tokens: int = SNIPPET_MAX_TOKENS) -> str:
    """Return ``text`` trimmed to at most ``max_tokens`` tiktoken tokens.

    No ellipsis is appended: the consumer is agent code, not a human UI,
    and adding a sentinel would inflate the token count it was meant to
    enforce.
    """

    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be positive; got {max_tokens}")
    enc = _encoder_singleton()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


@dataclass(frozen=True, slots=True)
class BudgetedItem:
    key: str
    snippet: str
    token_count: int


@dataclass(frozen=True, slots=True)
class BudgetResult:
    items: tuple[BudgetedItem, ...]
    truncated: bool


def apply_budget(
    items: list[BudgetedItem],
    *,
    total_budget: int,
) -> BudgetResult:
    """Drop trailing items once the cumulative token count exceeds ``total_budget``.

    Returns a new tuple of items that fits the budget plus a ``truncated``
    flag indicating whether any items were dropped. Per-snippet truncation
    is the caller's responsibility — this function never shortens an
    individual snippet, so upstream overages surface visibly rather than
    getting silently trimmed.
    """

    if total_budget <= 0:
        raise ValueError(f"total_budget must be positive; got {total_budget}")
    kept: list[BudgetedItem] = []
    used = 0
    truncated = False
    for item in items:
        if used + item.token_count > total_budget:
            truncated = True
            break
        kept.append(item)
        used += item.token_count
    if len(kept) < len(items):
        truncated = True
    return BudgetResult(items=tuple(kept), truncated=truncated)
