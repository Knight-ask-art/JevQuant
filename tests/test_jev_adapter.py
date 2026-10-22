"""Malformed Jev answers must fail closed for one signal, never crash a cycle."""

from __future__ import annotations

import httpx
import pytest

from jev_trading.adapters.models.jev import JevAdapter
from jev_trading.domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
)


def bare_adapter() -> JevAdapter:
    """``_normalize`` only needs the model name; no HTTP client is involved."""
    adapter = JevAdapter.__new__(JevAdapter)
    adapter.model_name = "jev-test"
    return adapter


def test_valid_answer_normalizes():
    result = bare_adapter()._normalize(
        {
            "model": "jev-test-1",
            "answers": {
                "entry_action": {
                    "choice": "buy",
                    "confidence": 0.72,
                    "probabilities": {"buy": 0.72, "wait": 0.2, "reject": 0.08},
                },
                "entry_quality": {"score": 3},
                "policy_fit": {"noul": 0.81},
            },
        },
        latency_ms=5,
    )
    assert result.entry_choice == "buy"
    assert result.entry_probability == pytest.approx(0.72)
    assert result.policy_fit.noul == pytest.approx(0.81)


@pytest.mark.parametrize(
    "answers",
    [
        {"entry_action": {"choice": "buy", "confidence": "very-high"}},
        {"entry_action": {"choice": "buy", "probabilities": {"buy": "high"}}},
        {"entry_action": {"choice": "buy", "probabilities": ["buy"]}},
        {"entry_action": ["buy"]},
        {"entry_action": {"choice": "buy", "confidence": float("nan")}},
        {"entry_action": {"choice": "buy"}, "entry_quality": ["strong"]},
        {"entry_action": {"choice": "buy"}, "policy_fit": {"noul": True}},
        {"entry_action": {"choice": "buy"}, "policy_fit": {"noul": "high"}},
        {"entry_action": {"choice": "buy"}, "entry_quality": {"score": -1}},
    ],
)
def test_malformed_answers_raise_contract_error(answers):
    with pytest.raises(DependencyBadResponseError):
        bare_adapter()._normalize({"answers": answers}, latency_ms=0)


def test_missing_answers_raises_contract_error():
    with pytest.raises(DependencyBadResponseError):
        bare_adapter()._normalize({}, latency_ms=0)


# -- the call deadline -------------------------------------------------------
# httpx bounds one attempt; a retry after a timeout would double the call.
# The adapter bounds the whole call instead, which is what keeps the
# signal's JEV_EVALUATING state -- and the streaming position -- finite.


def _deadline_adapter(timeout: float, attempts: list[float]):
    """An adapter whose POST sleeps ``attempts[n]`` on its n-th call."""

    import asyncio
    import time

    adapter = JevAdapter.__new__(JevAdapter)
    adapter.base_url = "https://jev.invalid"
    adapter.path = "/v1/decisions"
    adapter._api_key = "test-key"
    adapter.model_name = "jev-test"
    adapter.timeout = timeout
    adapter.max_retries = len(attempts)
    calls = {"n": 0}

    class Slowly:
        async def post(self, *args, **kwargs):
            delay = attempts[min(calls["n"], len(attempts) - 1)]
            calls["n"] += 1
            await asyncio.sleep(delay)
            raise httpx.ReadTimeout("slow")

    adapter._client = Slowly()
    return adapter, lambda: time.monotonic()


def test_a_hung_call_is_aborted_at_the_deadline():
    import asyncio
    import time

    adapter, clock = _deadline_adapter(0.2, [5.0, 5.0])
    started = clock()

    with pytest.raises(DependencyUnavailableError):
        asyncio.run(adapter._post({}))

    # One bounded attempt, not two: the retry never gets a fresh budget.
    assert time.monotonic() - started < 1.0


def test_the_retry_shares_one_deadline_rather_than_doubling_it():
    import asyncio
    import time

    # Two fast failures and a slow third: with a per-attempt timeout the
    # third would get another 0.3 s; with one deadline it is cut off.
    adapter, _ = _deadline_adapter(0.3, [0.01, 0.01, 5.0])
    started = time.monotonic()

    with pytest.raises(DependencyUnavailableError):
        asyncio.run(adapter._post({}))

    assert time.monotonic() - started < 1.0


def test_the_deadline_error_says_it_was_a_deadline():
    import asyncio

    adapter, _ = _deadline_adapter(0.05, [5.0])

    with pytest.raises(DependencyUnavailableError) as caught:
        asyncio.run(adapter._post({}))

    assert (caught.value.details or {}).get("reason") == "deadline"
