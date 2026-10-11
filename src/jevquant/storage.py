from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_rules_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    min_notional TEXT NOT NULL,
                    min_qty TEXT NOT NULL,
                    max_qty TEXT NOT NULL,
                    step_size TEXT NOT NULL,
                    min_price TEXT NOT NULL,
                    max_price TEXT NOT NULL,
                    tick_size TEXT NOT NULL,
                    raw_filters_json TEXT NOT NULL,
                    captured_at_utc TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_close_utc TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, candle_close_utc)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS model_signals (
                    signal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    candle_close_utc TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    action TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS risk_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    reason_code TEXT NOT NULL,
                    max_notional_usdt TEXT,
                    checks_detail_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    signal_id TEXT,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_qty TEXT,
                    filled_qty TEXT DEFAULT '0',
                    avg_fill_price TEXT,
                    fee_amount TEXT DEFAULT '0',
                    fee_asset TEXT,
                    raw_exchange_response TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
            """)

    def save_market_rules_snapshot(self, rules: Dict[str, Any]) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO market_rules_snapshots (
                    symbol, status, min_notional, min_qty, max_qty, step_size,
                    min_price, max_price, tick_size, raw_filters_json, captured_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rules["symbol"],
                rules["status"],
                str(rules["min_notional"]),
                str(rules["min_qty"]),
                str(rules["max_qty"]),
                str(rules["step_size"]),
                str(rules["min_price"]),
                str(rules["max_price"]),
                str(rules["tick_size"]),
                json.dumps(rules.get("raw_filters", {}), ensure_ascii=False),
                rules["captured_at_utc"]
            ))
            return cursor.lastrowid

    def get_latest_market_rules(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM market_rules_snapshots
                WHERE symbol = ?
                ORDER BY id DESC LIMIT 1
            """, (symbol,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "symbol": row["symbol"],
                "status": row["status"],
                "min_notional": Decimal(row["min_notional"]),
                "min_qty": Decimal(row["min_qty"]),
                "max_qty": Decimal(row["max_qty"]),
                "step_size": Decimal(row["step_size"]),
                "min_price": Decimal(row["min_price"]),
                "max_price": Decimal(row["max_price"]),
                "tick_size": Decimal(row["tick_size"]),
                "raw_filters": json.loads(row["raw_filters_json"]),
                "captured_at_utc": row["captured_at_utc"]
            }

    def save_candle(self, candle: Dict[str, Any]) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO candles (
                    symbol, timeframe, candle_close_utc, open, high, low, close, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candle["symbol"],
                candle["timeframe"],
                candle["candle_close_utc"],
                str(candle["open"]),
                str(candle["high"]),
                str(candle["low"]),
                str(candle["close"]),
                str(candle["volume"])
            ))

    def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY candle_close_utc DESC
                LIMIT ?
            """, (symbol, timeframe, limit))
            # Fetch the newest window, then return it in chronological order
            # so technical indicators can safely consume oldest -> newest.
            rows = list(reversed(cursor.fetchall()))
            return [
                {
                    "symbol": r["symbol"],
                    "timeframe": r["timeframe"],
                    "candle_close_utc": r["candle_close_utc"],
                    "open": Decimal(r["open"]),
                    "high": Decimal(r["high"]),
                    "low": Decimal(r["low"]),
                    "close": Decimal(r["close"]),
                    "volume": Decimal(r["volume"]),
                }
                for r in rows
            ]

    def save_order(self, order_data: Dict[str, Any]) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO orders (
                    client_order_id, signal_id, mode, symbol, side, order_type,
                    status, target_qty, filled_qty, avg_fill_price, fee_amount,
                    fee_asset, raw_exchange_response, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_data["client_order_id"],
                order_data.get("signal_id"),
                order_data["mode"],
                order_data["symbol"],
                order_data["side"],
                order_data["order_type"],
                order_data["status"],
                str(order_data.get("target_qty") or ""),
                str(order_data.get("filled_qty") or "0"),
                str(order_data.get("avg_fill_price") or ""),
                str(order_data.get("fee_amount") or "0"),
                order_data.get("fee_asset"),
                json.dumps(order_data.get("raw_exchange_response") or {}, ensure_ascii=False),
                order_data["created_at_utc"],
                order_data["updated_at_utc"],
            ))

    def get_order(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "client_order_id": row["client_order_id"],
                "signal_id": row["signal_id"],
                "mode": row["mode"],
                "symbol": row["symbol"],
                "side": row["side"],
                "order_type": row["order_type"],
                "status": row["status"],
                "target_qty": Decimal(row["target_qty"]) if row["target_qty"] else None,
                "filled_qty": Decimal(row["filled_qty"]) if row["filled_qty"] else Decimal("0"),
                "avg_fill_price": Decimal(row["avg_fill_price"]) if row["avg_fill_price"] else None,
                "fee_amount": Decimal(row["fee_amount"]) if row["fee_amount"] else Decimal("0"),
                "fee_asset": row["fee_asset"],
                "created_at_utc": row["created_at_utc"],
                "updated_at_utc": row["updated_at_utc"],
            }

    def get_unreconciled_orders(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM orders
                WHERE status IN ('ENTRY_SUBMITTING', 'EXIT_SUBMITTING', 'PARTIALLY_FILLED', 'UNKNOWN', 'OPEN', 'EXPIRED')
            """)
            rows = cursor.fetchall()
            return [
                {
                    "client_order_id": r["client_order_id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "status": r["status"],
                    "mode": r["mode"],
                }
                for r in rows
            ]
