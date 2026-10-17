"""Shared deterministic fixtures and builders for the test suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from jev_trading.config import Settings
from jev_trading.domain.enums import (
    IntentType,
    JevChoice,
    Mode,
    OrderPreference,
    Side,
)
from jev_trading.domain.intent import Provenance, TradeIntent
from jev_trading.domain.market import (
    AccountSnapshot,
    MarketSnapshot,
    SymbolFilters,
    SystemSnapshot,
)
from jev_trading.domain.policy import (
    ExitDefaults,
    JevGatePolicy,
    PolicyCompiler,
    PortfolioPolicy,
    Regime,
    StrategyPolicy,
    SymbolPolicy,
)
from jev_trading.domain.risk import RiskContext, compute_risk_snapshot_hash

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///./data/test.db", synthetic_market=True)


@pytest.fixture
def server_limits(settings: Settings):
    return settings.server_limits


def raw_policy(
    now: datetime = NOW,
    *,
    symbols: tuple[str, ...] = ("BTCUSDT",),
    notional: str = "1000",
    spread: str = "20",
    slippage: str = "20",
    cooldown: int = 0,
    expires_in: int = 900,
    mode_scope: tuple[Mode, ...] = (Mode.PAPER,),
    max_positions: int = 3,
) -> StrategyPolicy:
    return StrategyPolicy(
        schema_version="strategy-policy/1.0",
        policy_id=uuid4(),
        generated_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=expires_in),
        mode_scope=list(mode_scope),
        regime=Regime(label="range", confidence=0.6, notes="test regime"),
        universe=[
            SymbolPolicy(
                symbol=symbol,
                allowed_sides=[Side.BUY],
                max_position_notional_usdt=Decimal(notional),
                max_spread_bps=Decimal(spread),
                max_slippage_bps=Decimal(slippage),
                entry_cooldown_seconds=cooldown,
            )
            for symbol in symbols
        ],
        portfolio=PortfolioPolicy(
            max_gross_exposure_pct=Decimal("20"),
            max_positions=max_positions,
            max_daily_loss_pct=Decimal("2"),
            max_drawdown_pct=Decimal("5"),
        ),
        jev_gate=JevGatePolicy(
            entry_choice=JevChoice.BUY,
            min_choice_probability=0.52,
            min_policy_fit=0.6,
            min_quality_score=2.4,
        ),
        exit_defaults=ExitDefaults(
            stop_loss_bps=Decimal("120"),
            take_profit_bps=Decimal("240"),
            max_holding_seconds=3600,
        ),
        invalidation=["test invalidation"],
        rationale="Deterministic test policy.",
    )


def compiled_policy(server_limits, now: datetime = NOW, **kwargs):
    return PolicyCompiler(server_limits).compile(
        raw_policy(now, **kwargs), now=now, mode=Mode.PAPER
    )


def symbol_rules(
    *,
    symbol: str = "BTCUSDT",
    status: str = "TRADING",
    min_notional: str = "5",
    min_qty: str = "0.00001",
    step_size: str = "0.00001",
    tick_size: str = "0.01",
) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        status=status,
        base_asset=symbol[:-4],
        quote_asset="USDT",
        min_qty=Decimal(min_qty),
        max_qty=Decimal("9000"),
        step_size=Decimal(step_size),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        tick_size=Decimal(tick_size),
        min_notional=Decimal(min_notional),
        taker_fee_bps=Decimal("10"),
        maker_fee_bps=Decimal("10"),
    )


def market_snapshot(
    now: datetime = NOW,
    *,
    symbol: str = "BTCUSDT",
    bid: str = "67990",
    ask: str = "68010",
    stale_ms: int = 0,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        as_of=now - timedelta(milliseconds=stale_ms),
        source="synthetic",
        bid=Decimal(bid),
        ask=Decimal(ask),
        candle_close_at=now - timedelta(minutes=1),
        features={"sma_20": 67800.0, "sma_60": 67500.0, "rsi_14": 55.0, "atr_14": 120.0},
        is_synthetic=True,
    )


def account_snapshot(
    *, available: str = "10000", equity: str = "10000", gross: str = "0", open_positions: int = 0
) -> AccountSnapshot:
    return AccountSnapshot(
        equity_usdt=Decimal(equity),
        available_quote_usdt=Decimal(available),
        gross_exposure_usdt=Decimal(gross),
        open_positions=open_positions,
    )


def system_snapshot(
    *, mode: Mode = Mode.PAPER, kill: bool = False, feed: bool = True, execution: bool = True,
    live_armed: bool = False,
) -> SystemSnapshot:
    return SystemSnapshot(
        mode=mode.value,
        live_armed=live_armed,
        kill_switch=kill,
        feed_healthy=feed,
        execution_healthy=execution,
        policy_fresh=True,
    )


def make_intent(
    policy,
    ctx: RiskContext,
    *,
    intent_type: IntentType = IntentType.OPEN,
    side: Side = Side.BUY,
    symbol: str = "BTCUSDT",
    quote_notional: str = "100",
    limit_price: str = "68010",
    mode: Mode = Mode.PAPER,
    account_id=None,
    slippage_bps: str = "2",
    ttl_seconds: int = 10,
    idempotency: str = "intent-test-0001",
    created_at: datetime | None = None,
) -> TradeIntent:
    created = created_at or ctx.now
    provisional = TradeIntent(
        intent_id=uuid4(),
        signal_id=uuid4(),
        policy_id=policy.policy_id,
        mode=mode,
        account_id=account_id or uuid4(),
        symbol=symbol,
        side=side,
        intent_type=intent_type,
        order_preference=OrderPreference.LIMIT,
        quote_notional=Decimal(quote_notional),
        max_slippage_bps=Decimal(slippage_bps),
        limit_price=Decimal(limit_price),
        stop_loss_bps=(
            policy.exit_defaults.stop_loss_bps if intent_type is IntentType.OPEN else None
        ),
        take_profit_bps=(
            policy.exit_defaults.take_profit_bps if intent_type is IntentType.OPEN else None
        ),
        max_holding_seconds=(
            policy.exit_defaults.max_holding_seconds
            if intent_type is IntentType.OPEN
            else None
        ),
        created_at=created,
        expires_at=created + timedelta(seconds=ttl_seconds),
        idempotency_key=idempotency,
        risk_snapshot_hash="pending",
        provenance=Provenance(
            llm_model="simulated",
            jev_model="simulated",
            policy_source="simulated",
            data_source="synthetic",
            simulated=True,
        ),
    )
    risk_hash = compute_risk_snapshot_hash(provisional, ctx)
    return provisional.model_copy(update={"risk_snapshot_hash": risk_hash})


def make_context(
    policy,
    now: datetime = NOW,
    *,
    market: MarketSnapshot | None = None,
    account: AccountSnapshot | None = None,
    system: SystemSnapshot | None = None,
    rules: SymbolFilters | None = None,
    position_qty: str = "0",
    last_entry_at: datetime | None = None,
    duplicate: bool = False,
) -> RiskContext:
    return RiskContext(
        policy=policy,
        account=account or account_snapshot(),
        market=market or market_snapshot(now),
        symbol_rules=rules or symbol_rules(),
        system=system or system_snapshot(),
        now=now,
        position_qty=Decimal(position_qty),
        last_entry_at=last_entry_at,
        duplicate_intent=duplicate,
        max_staleness_ms=5000,
        exit_slippage_bps=Decimal("8"),
    )
