"""Malformed Jev answers must fail closed for one signal, never crash a cycle."""

from __future__ import annotations

import pytest

from jev_trading.adapters.models.jev import JevAdapter
from jev_trading.domain.errors import DependencyBadResponseError


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
