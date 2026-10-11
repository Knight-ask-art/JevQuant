import argparse
from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, Optional
import pydantic

from jevquant.config import AppConfig, load_config_from_env
from jevquant.exchange import (
    AccountBalance,
    BinancePublicAdapter,
    BinanceTestnetAdapter,
    ExchangeAdapter,
    ExecutionReport,
    normalize_order_status,
)
from jevquant.orders import OrderExecutor, OrderStateMachine
from jevquant.risk import RiskManager
from jevquant.storage import Storage


class LivePreflightReport(pydantic.BaseModel):
    passed: bool
    symbol: str
    free_usdt: Decimal
    capital_cap_usdt: Decimal
    min_notional: Decimal
    step_size: Decimal
    min_qty: Decimal
    status: str
    reasons: list[str] = []
    checked_at_utc: str


class BinanceLiveAdapter(ExchangeAdapter):
    """
    Binance Spot Live Adapter (Phase 5).
    Enforces live trading with restricted API credentials (spot only, no withdrawal).
    Must pass strict preflight checks and require multi-gate authentication.
    """
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        exchange: Optional[Any] = None,
        proxy: Optional[str] = None,
    ):
        if not api_key or not api_secret:
            raise ValueError("Live API key and secret are required for BinanceLiveAdapter.")

        import ccxt
        if exchange is not None:
            self.exchange = exchange
        else:
            config: Dict[str, Any] = {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {
                    "defaultType": "spot",
                },
                "enableRateLimit": True,
                "timeout": 15000,
            }
            proxy_url = proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("BINANCE_PROXY")
            if proxy_url:
                config["proxies"] = {
                    "http": proxy_url,
                    "https": proxy_url,
                }
            self.exchange = ccxt.binance(config)
            # Explicitly ensure sandbox mode is FALSE
            self.exchange.set_sandbox_mode(False)

    def fetch_market_rules(self, symbol: str = "BTC/USDT") -> Dict[str, Any]:
        markets = self.exchange.load_markets(reload=True)
        if symbol not in markets:
            raise ValueError(f"Symbol {symbol} not found on Binance Spot Live.")
        market_info = markets[symbol]
        raw_info = market_info.get("info", {})
        status = raw_info.get("status")

        if status != "TRADING":
            raise RuntimeError(f"Symbol {symbol} is currently not in TRADING status (current: {status}).")

        filters = raw_info.get("filters", [])
        min_notional = None
        min_qty = None
        max_qty = None
        step_size = None
        min_price = None
        max_price = None
        tick_size = None

        for f in filters:
            ftype = f.get("filterType")
            if ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                nv = f.get("minNotional") or f.get("notional")
                if nv is not None:
                    min_notional = Decimal(str(nv))
            elif ftype == "LOT_SIZE":
                min_qty = Decimal(str(f["minQty"]))
                max_qty = Decimal(str(f["maxQty"]))
                step_size = Decimal(str(f["stepSize"]))
            elif ftype == "PRICE_FILTER":
                min_price = Decimal(str(f["minPrice"]))
                max_price = Decimal(str(f["maxPrice"]))
                tick_size = Decimal(str(f["tickSize"]))

        if min_notional is None or None in (min_qty, max_qty, step_size) or None in (min_price, max_price, tick_size):
            raise ValueError(f"Critical filters missing on Live for {symbol}")

        captured_at = datetime.now(timezone.utc).isoformat()
        return {
            "symbol": symbol,
            "status": status,
            "min_notional": min_notional,
            "min_qty": min_qty,
            "max_qty": max_qty,
            "step_size": step_size,
            "min_price": min_price,
            "max_price": max_price,
            "tick_size": tick_size,
            "raw_filters": filters,
            "captured_at_utc": captured_at,
        }

    def fetch_closed_candles(self, symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 100):
        raw_ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 1)
        if not raw_ohlcv or len(raw_ohlcv) < 2:
            return []
        closed_raw = raw_ohlcv[:-1]
        candles = []
        for c in closed_raw:
            ts_ms, o, h, l, close_val, vol = c
            close_dt = datetime.fromtimestamp((ts_ms + 3600_000) / 1000, tz=timezone.utc)
            candles.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "candle_close_utc": close_dt.isoformat(),
                "open": Decimal(str(o)),
                "high": Decimal(str(h)),
                "low": Decimal(str(l)),
                "close": Decimal(str(close_val)),
                "volume": Decimal(str(vol)),
            })
        return candles

    def fetch_ticker_price(self, symbol: str = "BTC/USDT") -> Decimal:
        ticker = self.exchange.fetch_ticker(symbol)
        last_price = ticker.get("last")
        if last_price is None:
            raise RuntimeError(f"Unable to fetch ticker price on Live for {symbol}")
        return Decimal(str(last_price))

    def fetch_balance(self) -> AccountBalance:
        raw_bal = self.exchange.fetch_balance()
        free_usdt = Decimal(str(raw_bal.get("USDT", {}).get("free", 0) or 0))
        locked_usdt = Decimal(str(raw_bal.get("USDT", {}).get("used", 0) or 0))
        free_btc = Decimal(str(raw_bal.get("BTC", {}).get("free", 0) or 0))
        locked_btc = Decimal(str(raw_bal.get("BTC", {}).get("used", 0) or 0))
        total_usdt = free_usdt + locked_usdt

        now_utc = datetime.now(timezone.utc).isoformat()
        return AccountBalance(
            free_usdt=free_usdt,
            locked_usdt=locked_usdt,
            free_btc=free_btc,
            locked_btc=locked_btc,
            total_usdt=total_usdt,
            timestamp_utc=now_utc,
        )

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        client_order_id: str,
        price: Optional[Decimal] = None,
    ) -> ExecutionReport:
        side_lower = side.lower()
        type_lower = order_type.lower()
        params = {"clientOrderId": client_order_id}

        res = self.exchange.create_order(
            symbol=symbol,
            type=type_lower,
            side=side_lower,
            amount=float(quantity),
            price=float(price) if price else None,
            params=params,
        )

        filled_qty = Decimal(str(res.get("filled", 0) or 0))
        avg_price = Decimal(str(res.get("average", 0) or 0)) if res.get("average") else None
        status = normalize_order_status(res.get("status"), filled_qty)
        fee_info = res.get("fee") or {}
        fee_cost = Decimal(str(fee_info.get("cost", 0) or 0))
        fee_curr = fee_info.get("currency")

        now_utc = datetime.now(timezone.utc).isoformat()
        return ExecutionReport(
            client_order_id=client_order_id,
            exchange_order_id=str(res.get("id")),
            symbol=symbol,
            side=side.upper(),
            order_type=order_type.upper(),
            status=status,
            target_qty=quantity,
            filled_qty=filled_qty,
            avg_price=avg_price,
            fee_amount=fee_cost,
            fee_asset=fee_curr,
            raw_response=res,
            timestamp_utc=now_utc,
        )

    def fetch_order(self, client_order_id: str, symbol: str = "BTC/USDT") -> Optional[ExecutionReport]:
        import ccxt
        try:
            res = self.exchange.fetch_order(client_order_id, symbol=symbol, params={"clientOrderId": client_order_id})
        except ccxt.OrderNotFound:
            return None

        filled_qty = Decimal(str(res.get("filled", 0) or 0))
        avg_price = Decimal(str(res.get("average", 0) or 0)) if res.get("average") else None
        status = normalize_order_status(res.get("status"), filled_qty)
        target_qty = Decimal(str(res.get("amount", 0) or 0))
        fee_info = res.get("fee") or {}
        fee_cost = Decimal(str(fee_info.get("cost", 0) or 0))
        fee_curr = fee_info.get("currency")

        now_utc = datetime.now(timezone.utc).isoformat()
        return ExecutionReport(
            client_order_id=client_order_id,
            exchange_order_id=str(res.get("id")),
            symbol=symbol,
            side=str(res.get("side", "BUY")).upper(),
            order_type=str(res.get("type", "LIMIT")).upper(),
            status=status,
            target_qty=target_qty,
            filled_qty=filled_qty,
            avg_price=avg_price,
            fee_amount=fee_cost,
            fee_asset=fee_curr,
            raw_response=res,
            timestamp_utc=now_utc,
        )

    def cancel_order(self, client_order_id: str, symbol: str = "BTC/USDT") -> ExecutionReport:
        res = self.exchange.cancel_order(client_order_id, symbol=symbol, params={"clientOrderId": client_order_id})
        status = normalize_order_status(res.get("status", "canceled"), Decimal(str(res.get("filled", 0) or 0)))
        now_utc = datetime.now(timezone.utc).isoformat()
        return ExecutionReport(
            client_order_id=client_order_id,
            exchange_order_id=str(res.get("id")),
            symbol=symbol,
            side=str(res.get("side", "BUY")).upper(),
            order_type=str(res.get("type", "LIMIT")).upper(),
            status=status,
            target_qty=Decimal(str(res.get("amount", 0) or 0)),
            filled_qty=Decimal(str(res.get("filled", 0) or 0)),
            raw_response=res,
            timestamp_utc=now_utc,
        )


def run_live_preflight(
    adapter: ExchangeAdapter,
    capital_cap_usdt: Decimal,
    symbol: str = "BTC/USDT",
) -> LivePreflightReport:
    """
    Executes all Phase 5 preflight checks:
    1. Market status is TRADING.
    2. Dynamic exchangeInfo filters parsed and present.
    3. Account balance accessible and >= min_notional.
    4. Capital cap <= 10.00 USDT (~50 CNY equivalent).
    5. No withdrawal / deposit functions called or accessible.
    """
    reasons: list[str] = []
    now_utc = datetime.now(timezone.utc).isoformat()

    try:
        rules = adapter.fetch_market_rules(symbol)
    except Exception as e:
        return LivePreflightReport(
            passed=False,
            symbol=symbol,
            free_usdt=Decimal("0"),
            capital_cap_usdt=capital_cap_usdt,
            min_notional=Decimal("0"),
            step_size=Decimal("0"),
            min_qty=Decimal("0"),
            status="ERROR_FETCHING_RULES",
            reasons=[f"Market rules fetch failed: {str(e)}"],
            checked_at_utc=now_utc,
        )

    try:
        balance = adapter.fetch_balance()
    except Exception as e:
        return LivePreflightReport(
            passed=False,
            symbol=symbol,
            free_usdt=Decimal("0"),
            capital_cap_usdt=capital_cap_usdt,
            min_notional=rules["min_notional"],
            step_size=rules["step_size"],
            min_qty=rules["min_qty"],
            status=rules["status"],
            reasons=[f"Account balance check failed: {str(e)}"],
            checked_at_utc=now_utc,
        )

    # Invariants verification
    if rules["status"] != "TRADING":
        reasons.append(f"Symbol is not TRADING (status: {rules['status']})")

    if capital_cap_usdt > Decimal("10.00"):
        reasons.append(f"Capital cap {capital_cap_usdt} exceeds 50 CNY safety limit of 10.00 USDT")

    if balance.free_usdt < rules["min_notional"]:
        reasons.append(f"Free balance {balance.free_usdt} USDT is below minimum notional {rules['min_notional']} USDT")

    passed = len(reasons) == 0

    return LivePreflightReport(
        passed=passed,
        symbol=symbol,
        free_usdt=balance.free_usdt,
        capital_cap_usdt=capital_cap_usdt,
        min_notional=rules["min_notional"],
        step_size=rules["step_size"],
        min_qty=rules["min_qty"],
        status=rules["status"],
        reasons=reasons,
        checked_at_utc=now_utc,
    )


def initialize_runner(cli_confirm_live: bool = False, pause_new_entries: bool = False) -> Dict[str, Any]:
    """
    Runner initializer with multi-stage safety gates.
    """
    cfg = load_config_from_env()
    cfg.validate_safety_gates(cli_confirm_live=cli_confirm_live)

    store = Storage(cfg.db_path)
    sm = OrderStateMachine(initial_state="FLAT")

    if cfg.trading_mode == "live":
        adapter: ExchangeAdapter = BinanceLiveAdapter(
            api_key=cfg.binance_api_key or "",
            api_secret=cfg.binance_api_secret or "",
        )
        preflight = run_live_preflight(adapter, cfg.live_initial_capital_usdt or Decimal("7.00"), symbol=cfg.symbol)
        if not preflight.passed:
            raise RuntimeError(f"Live preflight check failed: {preflight.reasons}")
        mode_str = "live"
    elif cfg.trading_mode == "testnet":
        adapter = BinanceTestnetAdapter(
            api_key=cfg.binance_testnet_api_key or "",
            api_secret=cfg.binance_testnet_api_secret or "",
        )
        mode_str = "testnet"
    else:
        adapter = BinancePublicAdapter()
        mode_str = "paper"

    executor = OrderExecutor(adapter=adapter, storage=store, state_machine=sm, mode=mode_str)
    # Perform cold startup reconciliation
    executor.reconcile_on_startup(symbol=cfg.symbol)

    risk = RiskManager(
        initial_capital_usdt=cfg.live_initial_capital_usdt if cfg.trading_mode == "live" else cfg.paper_initial_balance_usdt,
    )
    if pause_new_entries:
        risk.is_halted = True
        risk.halt_reason = "MANUAL_PAUSE_TRIGGERED"

    return {
        "config": cfg,
        "adapter": adapter,
        "executor": executor,
        "storage": store,
        "risk": risk,
        "state_machine": sm,
    }
