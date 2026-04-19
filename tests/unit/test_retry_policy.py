"""Retry-policy classification table."""

from __future__ import annotations

import pytest

from tessera.adapters.errors import (
    AdapterAuthError,
    AdapterModelNotFoundError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.retrieval.retry_policy import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    RetryDecision,
    decide,
)


@pytest.mark.unit
def test_network_error_retries_with_first_backoff_after_one_attempt() -> None:
    assert decide(AdapterNetworkError("boom"), attempts=1) == RetryDecision(
        should_retry=True, delay_seconds=BACKOFF_SECONDS[0]
    )


@pytest.mark.unit
def test_oom_error_retries_with_later_backoff() -> None:
    assert decide(AdapterOOMError("oom"), attempts=2).delay_seconds == BACKOFF_SECONDS[1]


@pytest.mark.unit
def test_network_error_after_cap_does_not_retry() -> None:
    decision = decide(AdapterNetworkError("boom"), attempts=MAX_ATTEMPTS)
    assert decision.should_retry is False
    assert decision.delay_seconds == 0.0


@pytest.mark.unit
def test_model_not_found_is_terminal_from_the_first_attempt() -> None:
    decision = decide(AdapterModelNotFoundError("missing"), attempts=1)
    assert decision.should_retry is False


@pytest.mark.unit
def test_auth_error_is_terminal() -> None:
    assert decide(AdapterAuthError("401"), attempts=1).should_retry is False


@pytest.mark.unit
def test_response_error_is_terminal() -> None:
    assert decide(AdapterResponseError("bad shape"), attempts=1).should_retry is False


@pytest.mark.unit
def test_backoff_clamps_to_last_entry_for_high_attempts() -> None:
    # attempts=MAX_ATTEMPTS-1 uses the last backoff slot (index len-1).
    decision = decide(AdapterNetworkError("boom"), attempts=MAX_ATTEMPTS - 1)
    assert decision.delay_seconds == BACKOFF_SECONDS[-1]


@pytest.mark.unit
def test_zero_attempts_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        decide(AdapterNetworkError("boom"), attempts=0)
