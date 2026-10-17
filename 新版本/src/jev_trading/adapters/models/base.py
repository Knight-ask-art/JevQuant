"""Model-plane contracts: policy generation and typed Jev evaluation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class PolicyContext(BaseModel):
    """Account-safe derived state handed to the LLM. Never contains keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    as_of: AwareDatetime
    mode: str
    account_state: dict[str, Any]
    market_summary: dict[str, Any]
    absolute_server_limits: dict[str, Any]
    required_schema_version: str = "strategy-policy/1.0"


class PolicyGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw: dict[str, Any]
    model: str
    latency_ms: int
    usage: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = False
    source: str = "unknown"


class ChoiceAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    choice: str
    confidence: float = Field(ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)


class ScoreAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float


class NoulAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    noul: float = Field(ge=0.0, le=1.0)


class JevResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    evaluation_id: str
    entry_action: ChoiceAnswer
    entry_quality: ScoreAnswer
    policy_fit: NoulAnswer
    latency_ms: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def entry_choice(self) -> str:
        return self.entry_action.choice.lower()

    @property
    def entry_probability(self) -> float:
        probabilities = self.entry_action.probabilities
        if self.entry_choice in probabilities:
            return float(probabilities[self.entry_choice])
        return float(self.entry_action.confidence)


class PolicyGenerator(Protocol):
    async def generate_policy(self, context: PolicyContext) -> PolicyGenerationResult: ...

    async def health(self) -> dict[str, object]: ...


class JevEvaluator(Protocol):
    model_name: str

    async def evaluate(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        signal_id: str | None = None,
    ) -> JevResult: ...

    async def health(self) -> dict[str, object]: ...


def build_jev_questions() -> dict[str, Any]:
    """The bounded, machine-oriented question set Jev must answer."""
    return {
        "entry_action": {
            "type": "choice",
            "instructions": "Choose whether this snapshot merits a new entry under the policy.",
            "criteria": {
                "buy": "A BUY entry is justified now.",
                "wait": "The setup may be valid but timing is insufficient.",
                "reject": "The setup should not be traded.",
            },
        },
        "entry_quality": {
            "type": "score",
            "instructions": "Score setup quality.",
            "criteria": ["invalid", "weak", "acceptable", "strong", "exceptional"],
        },
        "policy_fit": {
            "type": "noul",
            "instructions": "Probability that this opportunity fits the active policy.",
        },
    }


def utc_iso(value: datetime) -> str:
    return value.isoformat()
