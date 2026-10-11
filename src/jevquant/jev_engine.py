from datetime import datetime, timezone
from decimal import Decimal
import os
import time
from typing import Any, Dict, Optional
import pydantic


class JevDecision(pydantic.BaseModel):
    action: str  # BUY, SELL, HOLD
    confidence: Optional[float] = None
    probabilities: Optional[Dict[str, float]] = None
    latency_ms: int
    model_name: str
    reason_code: str
    raw_response: Optional[Dict[str, Any]] = None
    timestamp_utc: str


class JevEngine:
    """
    TypeSafe Jev SDK decision adapter.
    Encapsulates calls to TypeSafeClient.system_one using structured Choice questions.
    Fails closed: Any failure, missing key, or network issue safely falls back to HOLD.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "jev-latest",
        timeout_seconds: float = 8.0,
    ):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY")
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

    def decide(
        self,
        market_state: Dict[str, Any],
    ) -> JevDecision:
        """
        Queries TypeSafe Jev for directional advice (BUY, SELL, HOLD).
        If the API key is not provided or invocation fails, returns HOLD with audit code.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        if not self.api_key:
            return JevDecision(
                action="HOLD",
                confidence=None,
                probabilities=None,
                latency_ms=0,
                model_name=self.model_name,
                reason_code="NO_API_KEY_FALLBACK_HOLD",
                raw_response=None,
                timestamp_utc=now_utc,
            )

        start_time = time.perf_counter()

        try:
            from typesafe_sdk import Choice, TypeSafeClient

            # Format state safely, ensuring all Decimal fields are converted to float/str
            safe_state = {}
            for k, v in market_state.items():
                if isinstance(v, Decimal):
                    safe_state[k] = float(v)
                else:
                    safe_state[k] = v

            with TypeSafeClient(api_key=self.api_key, model=self.model_name, timeout=self.timeout_seconds) as client:
                response = client.system_one(
                    state=safe_state,
                    questions={
                        "action": Choice(
                            instructions="基于已收盘的历史市场状态选择下一周期的研究信号(BUY/SELL/HOLD)。不决定金额或交易权限。",
                            criteria={
                                "BUY": "当上升趋势明确且风险回报比有利时建议现货买入",
                                "SELL": "当趋势转弱或下行风险加剧时建议平仓获利或止损",
                                "HOLD": "当趋势不明、震荡或缺乏安全边际时保持观望",
                            },
                        )
                    },
                )

            latency = int((time.perf_counter() - start_time) * 1000)
            action_choice = response.choices.get("action") if hasattr(response, "choices") else None
            
            if action_choice is not None:
                action = getattr(action_choice, "choice", "HOLD")
                confidence = getattr(action_choice, "confidence", None)
                probabilities = getattr(action_choice, "probabilities", None)
            else:
                action = "HOLD"
                confidence = None
                probabilities = None

            if action not in ("BUY", "SELL", "HOLD"):
                action = "HOLD"

            return JevDecision(
                action=action,
                confidence=confidence,
                probabilities=probabilities,
                latency_ms=latency,
                model_name=self.model_name,
                reason_code="JEV_DECISION_SUCCESS",
                raw_response={
                    "choice": action,
                    "confidence": confidence,
                    "probabilities": probabilities,
                },
                timestamp_utc=now_utc,
            )

        except Exception as e:
            latency = int((time.perf_counter() - start_time) * 1000)
            return JevDecision(
                action="HOLD",
                confidence=None,
                probabilities=None,
                latency_ms=latency,
                model_name=self.model_name,
                reason_code=f"JEV_ERROR_FALLBACK_HOLD: {type(e).__name__}",
                raw_response={"error": str(e)},
                timestamp_utc=now_utc,
            )
