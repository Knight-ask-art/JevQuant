from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
import os
from typing import Any, Dict, List, Optional
import ccxt
import pydantic


class ExecutionReport(pydantic.BaseModel):
    client_order_id: str
    exchange_order_id: Optional[str] = None
    symbol: str
    side: str  # BUY, SELL
    order_type: str  # LIMIT, MARKET
    status: str  # FILLED, PARTIALLY_FILLED, CANCELED, REJECTED, UNKNOWN
    target_qty: Decimal
    filled_qty: Decimal = Decimal("0")
    avg_price: Optional[Decimal] = None
    fee_amount: Decimal = Decimal("0")
    fee_asset: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    timestamp_utc: str


def normalize_order_status(status: Any, filled_qty: Decimal = Decimal("0")) -> str:
    """Map CCXT unified statuses to internal states, preserving partial fills."""
    normalized = str(status or "").strip().lower()
    if normalized in ("closed", "filled"):
        return "FILLED"
    if normalized in ("canceled", "cancelled", "expired", "rejected"):
        if filled_qty > 0:
            return "PARTIALLY_FILLED"
        return {"canceled": "CANCELED", "cancelled": "CANCELED",
                "expired": "EXPIRED", "rejected": "REJECTED"}[normalized]
    if normalized in ("partially_filled", "partially-filled", "partial"):
        return "PARTIALLY_FILLED"
    if normalized in ("open", "new", "pending"):
        return "PARTIALLY_FILLED" if filled_qty > 0 else "OPEN"
    return "UNKNOWN"


class AccountBalance(pydantic.BaseModel):
    free_usdt: Decimal
    locked_usdt: Decimal
    free_btc: Decimal
    locked_btc: Decimal
    total_usdt: Decimal
    timestamp_utc: str


class ExchangeAdapter(ABC):
    @abstractmethod
    def fetch_market_rules(self, symbol: str = "BTC/USDT") -> Dict[str, Any]:
        pass

    @abstractmethod
    def fetch_closed_candles(self, symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 100) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def fetch_ticker_price(self, symbol: str = "BTC/USDT") -> Decimal:
        pass

    @abstractmethod
    def fetch_balance(self) -> AccountBalance:
        pass

    @abstractmethod
    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        client_order_id: str,
        price: Optional[Decimal] = None,
    ) -> ExecutionReport:
        pass

    @abstractmethod
    def fetch_order(self, client_order_id: str, symbol: str = "BTC/USDT") -> Optional[ExecutionReport]:
        pass

    @abstractmethod
    def cancel_order(self, client_order_id: str, symbol: str = "BTC/USDT") -> ExecutionReport:
        pass


class BinancePublicAdapter(ExchangeAdapter):
    """
    Adapter for Binance Spot public market data and trading rules.
    Operates strictly in read-only mode without any private API keys.
    """
    def __init__(self, exchange: Optional[ccxt.binance] = None, proxy: Optional[str] = None):
        if exchange is not None:
            self.exchange = exchange
        else:
            config = {
                "options": {"defaultType": "spot"},
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

    def fetch_market_rules(self, symbol: str = "BTC/USDT") -> Dict[str, Any]:
        try:
            markets = self.exchange.load_markets(reload=True)
        except ccxt.ExchangeNotAvailable as e:
            if "451" in str(e) or "restricted location" in str(e).lower():
                raise PermissionError(
                    f"Binance API access restricted (HTTP 451: Restricted location). "
                    f"Per JevQuant.spec rules, live/online trading is halted when local region is disallowed. "
                    f"Configure proxy or use Paper mode with simulated rules."
                ) from e
            raise

        if symbol not in markets:
            raise ValueError(f"Symbol {symbol} not found on Binance Spot.")

        market_info = markets[symbol]
        raw_info = market_info.get("info", {})
        status = raw_info.get("status")

        if status != "TRADING":
            raise RuntimeError(f"Symbol {symbol} is currently not in TRADING status (current: {status}).")

        filters = raw_info.get("filters", [])
        if not filters:
            raise RuntimeError(f"No exchange filters returned for {symbol}.")

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
                notional_val = f.get("minNotional") or f.get("notional")
                if notional_val is not None:
                    min_notional = Decimal(str(notional_val))
            elif ftype == "LOT_SIZE":
                min_qty = Decimal(str(f["minQty"]))
                max_qty = Decimal(str(f["maxQty"]))
                step_size = Decimal(str(f["stepSize"]))
            elif ftype == "PRICE_FILTER":
                min_price = Decimal(str(f["minPrice"]))
                max_price = Decimal(str(f["maxPrice"]))
                tick_size = Decimal(str(f["tickSize"]))

        if min_notional is None:
            raise ValueError(f"Critical filter MIN_NOTIONAL missing for {symbol}")
        if None in (min_qty, max_qty, step_size):
            raise ValueError(f"Critical filter LOT_SIZE missing for {symbol}")
        if None in (min_price, max_price, tick_size):
            raise ValueError(f"Critical filter PRICE_FILTER missing for {symbol}")

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

    def fetch_closed_candles(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
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
            raise RuntimeError(f"Unable to fetch ticker price for {symbol}")
        return Decimal(str(last_price))

    def fetch_balance(self) -> AccountBalance:
        raise NotImplementedError("Public adapter has no private API keys to fetch account balances.")

    def create_order(self, symbol: str, side: str, order_type: str, quantity: Decimal, client_order_id: str, price: Optional[Decimal] = None) -> ExecutionReport:
        raise PermissionError("Public adapter strictly prohibits order placement.")

    def fetch_order(self, client_order_id: str, symbol: str = "BTC/USDT") -> Optional[ExecutionReport]:
        raise NotImplementedError("Public adapter cannot query private orders.")

    def cancel_order(self, client_order_id: str, symbol: str = "BTC/USDT") -> ExecutionReport:
        raise NotImplementedError("Public adapter cannot cancel private orders.")


class BinanceTestnetAdapter(ExchangeAdapter):
    """
    Binance Spot Testnet Adapter (Phase 4).
    Uses isolated Testnet endpoints and dedicated Testnet credentials.
    Supports create_order with custom client_order_id, query, and cancel.
    """
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        exchange: Optional[ccxt.binance] = None,
        proxy: Optional[str] = None,
    ):
        if not api_key or not api_secret:
            raise ValueError("Testnet API key and secret are required for BinanceTestnetAdapter.")

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
            # Dedicated Binance Spot Testnet base URL
            config["urls"] = {
                "api": {
                    "public": "https://testnet.binance.vision/api",
                    "private": "https://testnet.binance.vision/api",
                }
            }
            proxy_url = proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("BINANCE_PROXY")
            if proxy_url:
                config["proxies"] = {
                    "http": proxy_url,
                    "https": proxy_url,
                }
            self.exchange = ccxt.binance(config)
            # Enable sandbox mode for testnet
            self.exchange.set_sandbox_mode(True)

    def fetch_market_rules(self, symbol: str = "BTC/USDT") -> Dict[str, Any]:
        """Read actual Spot Testnet exchange filters; never substitute defaults.

        Testnet and public Spot use the same exchangeInfo filter schema.
        Reusing the strict parser prevents fabricated trading limits or a
        missing status from silently enabling orders.
        """
        return BinancePublicAdapter(exchange=self.exchange).fetch_market_rules(symbol)

    def fetch_closed_candles(self, symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 100) -> List[Dict[str, Any]]:
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
            raise RuntimeError(f"Unable to fetch ticker price on Testnet for {symbol}")
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

        # Fee parsing
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
