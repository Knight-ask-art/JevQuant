"""Market data and exchange-rule domain objects.

Filters are always discovered from ``exchangeInfo`` at runtime.  They are
never hardcoded per symbol.  When filters are missing or the symbol is not
TRADING, the system refuses to trade (fail-closed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field

from .errors import ValidationAppError
from .money import ZERO, dec, quantize_down, round_price, safe_div


class SymbolFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    min_qty: Decimal = Field(gt=0)
    max_qty: Decimal = Field(gt=0)
    step_size: Decimal = Field(gt=0)
    min_price: Decimal = Field(gt=0)
    max_price: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    min_notional: Decimal = Field(gt=0)
    notional_apply_to_market: bool = True
    maker_fee_bps: Decimal = Field(default=Decimal("10"), ge=0)
    taker_fee_bps: Decimal = Field(default=Decimal("10"), ge=0)

    def require_tradable(self) -> None:
        if self.status != "TRADING":
            raise ValidationAppError(
                f"Symbol {self.symbol} is not in TRADING status.",
                details={"symbol": self.symbol, "status": self.status},
            )

    def quantize_qty(self, qty: Decimal) -> Decimal:
        return quantize_down(qty, self.step_size)

    def quantize_price(self, price: Decimal) -> Decimal:
        return round_price(price, self.tick_size)

    def validate_qty(self, qty: Decimal) -> list[str]:
        problems: list[str] = []
        if qty <= ZERO:
            problems.append("qty must be positive")
            return problems
        if qty < self.min_qty:
            problems.append(f"qty {qty} below min_qty {self.min_qty}")
        if qty > self.max_qty:
            problems.append(f"qty {qty} above max_qty {self.max_qty}")
        if quantize_down(qty, self.step_size) != qty:
            problems.append(f"qty {qty} does not respect step_size {self.step_size}")
        return problems

    def validate_price(self, price: Decimal) -> list[str]:
        problems: list[str] = []
        if price <= ZERO:
            problems.append("price must be positive")
            return problems
        if price < self.min_price:
            problems.append(f"price {price} below min_price {self.min_price}")
        if price > self.max_price:
            problems.append(f"price {price} above max_price {self.max_price}")
        if round_price(price, self.tick_size) != price:
            problems.append(f"price {price} does not respect tick_size {self.tick_size}")
        return problems

    def notional(self, qty: Decimal, price: Decimal) -> Decimal:
        return qty * price

    def meets_min_notional(self, qty: Decimal, price: Decimal) -> bool:
        return self.notional(qty, price) >= self.min_notional

    @classmethod
    def from_binance_exchange_info(cls, payload: dict[str, Any], symbol: str) -> SymbolFilters:
        symbol = symbol.upper()
        for item in payload.get("symbols", []):
            if item.get("symbol") != symbol:
                continue
            filters = {f.get("filterType"): f for f in item.get("filters", [])}
            lot = filters.get("LOT_SIZE")
            price_filter = filters.get("PRICE_FILTER")
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            if not lot or not price_filter or not notional:
                raise ValidationAppError(
                    f"exchangeInfo is missing required filters for {symbol}.",
                    details={"symbol": symbol},
                )
            min_notional = notional.get("minNotional") or notional.get("notional")
            if min_notional is None:
                raise ValidationAppError(
                    f"exchangeInfo is missing minNotional for {symbol}.",
                    details={"symbol": symbol},
                )
            apply_to_market = notional.get("applyToMarket", True)
            return cls(
                symbol=symbol,
                status=item.get("status", "UNKNOWN"),
                base_asset=item.get("baseAsset", ""),
                quote_asset=item.get("quoteAsset", ""),
                min_qty=dec(lot["minQty"]),
                max_qty=dec(lot["maxQty"]),
                step_size=dec(lot["stepSize"]),
                min_price=dec(price_filter["minPrice"]),
                max_price=dec(price_filter["maxPrice"]),
                tick_size=dec(price_filter["tickSize"]),
                min_notional=dec(min_notional),
                notional_apply_to_market=bool(apply_to_market),
            )
        raise ValidationAppError(
            f"Symbol {symbol} was not found in exchangeInfo.",
            details={"symbol": symbol},
        )


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID = Field(default_factory=uuid4)
    symbol: str
    as_of: AwareDatetime
    source: str
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    candle_close_at: AwareDatetime
    features: dict[str, float] = Field(default_factory=dict)
    is_synthetic: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_bps(self) -> Decimal:
        return safe_div(self.ask - self.bid, self.mid) * Decimal("10000")

    def staleness_ms(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return max(0, int((now - self.as_of).total_seconds() * 1000))

    def is_fresh(self, now: datetime | None = None, max_age_ms: int = 5000) -> bool:
        return self.staleness_ms(now) <= max_age_ms

    def feature(self, name: str, default: float = 0.0) -> float:
        return float(self.features.get(name, default))


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    equity_usdt: Decimal
    available_quote_usdt: Decimal
    gross_exposure_usdt: Decimal
    open_positions: int = Field(ge=0)
    daily_realized_pnl_usdt: Decimal = ZERO
    daily_realized_pnl_pct: Decimal = ZERO
    drawdown_pct: Decimal = ZERO


class SystemSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    live_armed: bool
    kill_switch: bool
    feed_healthy: bool
    execution_healthy: bool
    policy_fresh: bool
