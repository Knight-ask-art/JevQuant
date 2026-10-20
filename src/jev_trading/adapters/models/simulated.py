"""Deterministic simulated model plane for local paper demos.

Every artifact produced here is marked ``simulated=True`` and shows up as
such in provenance, the audit trail and the UI.  It exists so the full
pipeline can be exercised without provider keys - it is not alpha.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ...domain.enums import JevChoice, Mode, RegimeLabel, Side
from ...domain.policy import (
    SCHEMA_VERSION,
    ExitDefaults,
    JevGatePolicy,
    PolicyCompiler,
    PortfolioPolicy,
    Regime,
    StrategyPolicy,
    SymbolPolicy,
)
from .base import (
    ChoiceAnswer,
    JevResult,
    NoulAnswer,
    PolicyContext,
    PolicyGenerationResult,
    ScoreAnswer,
)

SIMULATED_MODEL = "simulated-rule-based"


class SimulatedPolicyGenerator:
    """A deterministic stand-in for the Console LLM.

    It is rule-based, not adaptive: it scores the whole universe with a fixed
    momentum rule and lets the server compiler clamp whatever it proposes.
    """

    model_name = SIMULATED_MODEL

    def __init__(self, server_limits) -> None:
        self.compiler = PolicyCompiler(server_limits)
        self.server = server_limits

    async def generate_policy(
        self, context: PolicyContext, *, toolbox: Any | None = None
    ) -> PolicyGenerationResult:
        # The rule-based stand-in ignores tools and memory on purpose: it is
        # deterministic, and its output is always labelled simulated.
        started = time.perf_counter()
        now = datetime.now(UTC)
        symbols: dict[str, Any] = context.market_summary.get("symbols", {})
        returns = [float(item.get("return_15m_bps", 0.0)) for item in symbols.values()]
        ranges = [float(item.get("range_1h_bps", 0.0)) for item in symbols.values()]
        average_return = sum(returns) / len(returns) if returns else 0.0
        average_range = sum(ranges) / len(ranges) if ranges else 0.0
        if average_range > 500:
            label, notes = RegimeLabel.HIGH_VOL, "Volatility elevated; size down."
        elif average_return > 80:
            label, notes = RegimeLabel.TREND, "Upward drift; prefer pullback longs."
        elif average_return < -80:
            label, notes = RegimeLabel.RISK_OFF, "Downward drift; stay selective."
        else:
            label, notes = RegimeLabel.RANGE, "Range-bound; mean reversion only."

        allowed = sorted(self.server.allowed_symbols & set(symbols.keys()))
        if not allowed:
            allowed = sorted(self.server.allowed_symbols)
        # Stand-in for the model choosing its universe: keep the strongest
        # momentum names, and only when momentum is actually positive.
        ranked = sorted(
            (
                (symbol, float(symbols[symbol].get("return_15m_bps", 0.0)))
                for symbol in allowed
                if symbol in symbols
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        picked = [symbol for symbol, momentum in ranked[:2] if momentum > 0]
        if not picked:
            picked = [symbol for symbol, _ in ranked[:1]] or allowed[:1]
        per_symbol_cap = min(Decimal("300"), self.server.max_position_notional_usdt)
        universe = [
            SymbolPolicy(
                symbol=symbol,
                allowed_sides=[Side.BUY],
                max_position_notional_usdt=per_symbol_cap,
                max_spread_bps=Decimal("6"),
                max_slippage_bps=Decimal("12"),
                entry_cooldown_seconds=60,
            )
            for symbol in picked
        ]
        selection_note = (
            f"Rule-based selection kept {', '.join(picked)} of {', '.join(allowed)} "
            "by 15m momentum."
        )

        policy = StrategyPolicy(
            schema_version=SCHEMA_VERSION,
            policy_id=uuid4(),
            generated_at=now,
            expires_at=now + timedelta(minutes=30),
            mode_scope=[Mode.PAPER, Mode.TESTNET],
            regime=Regime(label=label, confidence=0.72, notes=notes),
            universe=universe,
            portfolio=PortfolioPolicy(
                max_gross_exposure_pct=Decimal("12"),
                max_positions=2,
                max_daily_loss_pct=Decimal("1"),
                max_drawdown_pct=Decimal("2.5"),
            ),
            jev_gate=JevGatePolicy(
                entry_choice=JevChoice.BUY,
                min_choice_probability=0.52,
                min_policy_fit=0.6,
                min_quality_score=2.4,
            ),
            exit_defaults=ExitDefaults(
                stop_loss_bps=Decimal("70"),
                take_profit_bps=Decimal("120"),
                max_holding_seconds=3600,
            ),
            invalidation=[
                "market_data_stale",
                "spread_above_policy",
                "daily_loss_limit_hit",
                "policy_expired",
            ],
            rationale=(
                "SIMULATED policy: deterministic rule-based stand-in used when no "
                "Console LLM credential is configured. It exists to exercise the "
                "risk, execution and audit pipeline end to end. " + selection_note
            ),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return PolicyGenerationResult(
            raw=policy.model_dump(mode="json"),
            model=SIMULATED_MODEL,
            latency_ms=latency_ms,
            usage={"input_tokens": 0, "output_tokens": 0},
            simulated=True,
            source="simulated",
            turns=0,
            trace={"tools_available": False, "tool_calls": []},
        )

    async def health(self) -> dict[str, object]:
        return {"healthy": True, "provider": "simulated", "model": SIMULATED_MODEL}


class SimulatedJevEvaluator:
    """Deterministic typed evaluator used when no Jev credential exists."""

    model_name = "jev-simulated-1.13"

    async def evaluate(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        signal_id: str | None = None,
    ) -> JevResult:
        started = time.perf_counter()
        symbol = str(state.get("symbol", "UNKNOWN"))
        candle = str(state.get("candle_close_at", ""))
        rng = random.Random(f"jev:{symbol}:{candle}:{state.get('policy_id', '')}")
        jitter = rng.gauss(0.0, 0.02)

        rsi = float(state.get("rsi_5m", 50.0))
        imbalance = float(state.get("orderbook_imbalance_10", 0.0))
        distance_vwap = float(state.get("distance_vwap_5m_bps", 0.0))
        spread = float(state.get("spread_bps", 2.0))
        return_5m = float(state.get("return_5m_bps", 0.0))

        rsi_edge = max(-1.0, min(1.0, (50.0 - rsi) / 25.0))
        mean_reversion = max(-1.0, min(1.0, -distance_vwap / 40.0))
        flow = max(-1.0, min(1.0, imbalance / 0.3))
        momentum_penalty = max(0.0, min(0.4, abs(return_5m) / 300.0))

        raw = 0.52 + 0.22 * rsi_edge + 0.14 * mean_reversion + 0.14 * flow
        raw -= momentum_penalty * 0.35
        raw -= max(0.0, (spread - 2.0) / 12.0)
        buy_probability = max(0.02, min(0.97, raw + jitter))
        wait_probability = (1.0 - buy_probability) * 0.7
        reject_probability = max(0.0, 1.0 - buy_probability - wait_probability)
        probabilities = {
            "buy": round(buy_probability, 4),
            "wait": round(wait_probability, 4),
            "reject": round(reject_probability, 4),
        }
        choice = max(probabilities, key=lambda key: probabilities[key])

        quality = max(
            0.0,
            min(
                5.0,
                0.9
                + 3.7 * buy_probability
                + 0.4 * max(0.0, rsi_edge)
                - 0.2 * max(0.0, spread - 2.0),
            ),
        )
        fit = max(0.0, min(1.0, 0.78 + 0.18 * buy_probability - 0.04 * (spread / 5.0)))

        return JevResult(
            model=self.model_name,
            evaluation_id=f"jev_sim_{uuid4().hex[:16]}",
            entry_action=ChoiceAnswer(
                choice=choice,
                confidence=probabilities[choice],
                probabilities=probabilities,
            ),
            entry_quality=ScoreAnswer(score=round(quality, 3)),
            policy_fit=NoulAnswer(noul=round(fit, 4)),
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage={"input_tokens": 0},
            simulated=True,
            raw={"simulated": True, "state_keys": sorted(state.keys())[:20]},
        )

    async def health(self) -> dict[str, object]:
        return {"healthy": True, "provider": "simulated", "model": self.model_name}
