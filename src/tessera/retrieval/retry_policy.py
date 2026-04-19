"""Embed-worker retry classification per docs/system-design.md §Failure taxonomy.

The embed worker calls :func:`decide` after each adapter failure to learn
(a) whether a retry should be scheduled and (b) how long to wait before it.
Keeping this as a closed-form function rather than baking the ladder into
the worker loop makes the policy testable in isolation and auditable from a
single file.

Retryable: network errors and OOM — both are transient provider states
(service bounce, GPU contention) that commonly clear inside the backoff
window. Terminal: response-schema errors (stable, won't clear on retry),
auth errors (rotation problem, human must intervene), and model-not-found
(the configured model is absent from the adapter; retrying is wasted work
until the user or a higher-level recovery does ``ollama pull``).
"""

from __future__ import annotations

from typing import Final, NamedTuple

from tessera.adapters.errors import (
    AdapterError,
    AdapterNetworkError,
    AdapterOOMError,
)

# Retry cap is total attempts including the initial call. Three retries
# after the first failure — matching "5s, 30s, 2min" from the plan — means
# four attempts total with three backoff intervals.
MAX_ATTEMPTS: Final[int] = 4

BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 30.0, 120.0)


class RetryDecision(NamedTuple):
    should_retry: bool
    delay_seconds: float


def decide(error: AdapterError, attempts: int) -> RetryDecision:
    """Decide whether a failed embed call should retry.

    ``attempts`` is the number of attempts already completed, including the
    one that raised ``error``. A fresh call that fails once passes
    ``attempts=1`` and receives a retry decision with the first backoff.
    """

    if attempts <= 0:
        raise ValueError(f"attempts must be positive; got {attempts}")
    if attempts >= MAX_ATTEMPTS:
        return RetryDecision(should_retry=False, delay_seconds=0.0)
    if not isinstance(error, AdapterNetworkError | AdapterOOMError):
        # Terminal classes (model-not-found, auth, response-shape) — retries
        # only defer the inevitable and burn provider quota along the way.
        return RetryDecision(should_retry=False, delay_seconds=0.0)
    idx = min(attempts - 1, len(BACKOFF_SECONDS) - 1)
    return RetryDecision(should_retry=True, delay_seconds=BACKOFF_SECONDS[idx])
