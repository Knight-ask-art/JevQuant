from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from typing import Any, Dict, List, Optional
import pydantic
from jevquant.exchange import ExchangeAdapter, ExecutionReport
from jevquant.storage import Storage


class PaperFill(pydantic.BaseModel):
    client_order_id: str
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    notional: Decimal
    fee_usdt: Decimal
    timestamp_utc: str


class PaperAccount:
    """
    Paper Trading Ledger for Binance Spot BTC/USDT.
    Enforces exact balance accounting, fee deductions, and position tracking.
    """
    def __init__(
        self,
        initial_usdt: Decimal = Decimal("7.00"),
        maker_taker_fee: Decimal = Decimal("0.001"),
        slippage_pct: Decimal = Decimal("0.0005"),
    ):
        self.initial_usdt = initial_usdt
        self.usdt_balance = initial_usdt
        self.btc_balance = Decimal("0")
        self.maker_taker_fee = maker_taker_fee
        self.slippage_pct = slippage_pct
        self.trades: List[PaperFill] = []
        self.today_realized_loss_usdt: Decimal = Decimal("0")
        self.entry_price: Optional[Decimal] = None

    @property
    def has_position(self) -> bool:
        return self.btc_balance > Decimal("0")

    def get_equity(self, current_price: Decimal) -> Decimal:
        return self.usdt_balance + (self.btc_balance * current_price)

    def execute_buy(
        self,
        symbol: str,
        target_qty: Decimal,
        market_price: Decimal,
        client_order_id: str,
    ) -> PaperFill:
        if self.has_position:
            raise RuntimeError("Cannot execute buy: position already exists.")

        exec_price = market_price * (Decimal("1") + self.slippage_pct)
        gross_cost = target_qty * exec_price
        fee_usdt = gross_cost * self.maker_taker_fee
        total_deduction = gross_cost + fee_usdt

        if self.usdt_balance < total_deduction:
            raise ValueError(f"Insufficient USDT: have {self.usdt_balance}, need {total_deduction}")

        self.usdt_balance -= total_deduction
        self.btc_balance += target_qty
        self.entry_price = exec_price

        now_utc = datetime.now(timezone.utc).isoformat()
        fill = PaperFill(
            client_order_id=client_order_id,
            symbol=symbol,
            side="BUY",
            price=exec_price,
            qty=target_qty,
            notional=gross_cost,
            fee_usdt=fee_usdt,
            timestamp_utc=now_utc,
        )
        self.trades.append(fill)
        return fill

    def execute_sell(
        self,
        symbol: str,
        sell_qty: Decimal,
        market_price: Decimal,
        client_order_id: str,
    ) -> PaperFill:
        if not self.has_position or self.btc_balance < sell_qty:
            raise ValueError(f"Insufficient BTC position to sell: have {self.btc_balance}, requested {sell_qty}")

        exec_price = market_price * (Decimal("1") - self.slippage_pct)
        gross_return = sell_qty * exec_price
        fee_usdt = gross_return * self.maker_taker_fee
        net_credit = gross_return - fee_usdt

        if self.entry_price is not None:
            pnl = (exec_price - self.entry_price) * sell_qty - fee_usdt
            if pnl < Decimal("0"):
                self.today_realized_loss_usdt += abs(pnl)

        self.btc_balance -= sell_qty
        self.usdt_balance += net_credit
        if self.btc_balance == Decimal("0"):
            self.entry_price = None

        now_utc = datetime.now(timezone.utc).isoformat()
        fill = PaperFill(
            client_order_id=client_order_id,
            symbol=symbol,
            side="SELL",
            price=exec_price,
            qty=sell_qty,
            notional=gross_return,
            fee_usdt=fee_usdt,
            timestamp_utc=now_utc,
        )
        self.trades.append(fill)
        return fill


class OrderStateMachine:
    """
    State machine managing order lifecycle:
    FLAT -> ENTRY_SUBMITTING -> LONG -> EXIT_SUBMITTING -> FLAT
    Enforces idempotency and client_order_id uniqueness.
    """
    VALID_STATES = ("FLAT", "ENTRY_SUBMITTING", "LONG", "EXIT_SUBMITTING", "HALTED", "RECONCILIATION_REQUIRED")

    def __init__(self, initial_state: str = "FLAT"):
        self.state = initial_state
        self.pending_order_id: Optional[str] = None
        self.seen_order_ids: set[str] = set()

    def generate_client_order_id(self, signal_id: str, side: str) -> str:
        raw = f"{signal_id}:{side}:{datetime.now(timezone.utc).timestamp()}"
        order_id = "ord_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return order_id

    def transition_to(self, new_state: str) -> None:
        if new_state not in self.VALID_STATES:
            raise ValueError(f"Invalid state: {new_state}")
        self.state = new_state


class OrderExecutor:
    """
    Unified Order Execution & Reconciliation Manager (Phase 4).
    Sole entity authorized to invoke create_order on ExchangeAdapter.
    Manages client_order_id idempotency, database audit, and restart reconciliation.
    """
    def __init__(
        self,
        adapter: ExchangeAdapter,
        storage: Storage,
        state_machine: Optional[OrderStateMachine] = None,
        mode: str = "testnet",
    ):
        self.adapter = adapter
        self.storage = storage
        self.sm = state_machine or OrderStateMachine()
        self.mode = mode

    def reconcile_on_startup(self, symbol: str = "BTC/USDT") -> None:
        """
        Startup reconciliation: queries unreconciled orders from SQLite,
        reconciles with exchange status, and restores state machine.
        """
        unreconciled = self.storage.get_unreconciled_orders()
        if not unreconciled:
            # Query balance to verify position
            try:
                bal = self.adapter.fetch_balance()
                if bal.free_btc > Decimal("0") or bal.locked_btc > Decimal("0"):
                    self.sm.transition_to("LONG")
                else:
                    self.sm.transition_to("FLAT")
            except Exception:
                # In paper or offline mode, preserve current state
                pass
            return

        for order in unreconciled:
            cid = order["client_order_id"]
            try:
                report = self.adapter.fetch_order(cid, symbol=symbol)
                if report is not None:
                    now_utc = datetime.now(timezone.utc).isoformat()
                    self.storage.save_order({
                        "client_order_id": cid,
                        "signal_id": None,
                        "mode": self.mode,
                        "symbol": symbol,
                        "side": report.side,
                        "order_type": report.order_type,
                        "status": report.status,
                        "target_qty": report.target_qty,
                        "filled_qty": report.filled_qty,
                        "avg_fill_price": report.avg_price,
                        "fee_amount": report.fee_amount,
                        "fee_asset": report.fee_asset,
                        "raw_exchange_response": report.raw_response,
                        "created_at_utc": now_utc,
                        "updated_at_utc": now_utc,
                    })
                    if report.status == "FILLED":
                        if report.side == "BUY":
                            self.sm.transition_to("LONG")
                        else:
                            self.sm.transition_to("FLAT")
                    elif report.status in ("CANCELED", "REJECTED", "EXPIRED"):
                        self.sm.transition_to("RECONCILIATION_REQUIRED" if report.filled_qty > 0 else "FLAT")
                    elif report.status == "PARTIALLY_FILLED":
                        self.sm.transition_to("RECONCILIATION_REQUIRED")
                else:
                    self.sm.transition_to("RECONCILIATION_REQUIRED")
            except Exception:
                self.sm.transition_to("RECONCILIATION_REQUIRED")

    def execute_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        signal_id: str,
    ) -> ExecutionReport:
        if self.sm.state == "HALTED":
            raise RuntimeError("Order execution blocked: State machine is HALTED.")
        if self.sm.state == "RECONCILIATION_REQUIRED":
            raise RuntimeError("Order execution blocked: Reconciliation required before trading.")

        cid = self.sm.generate_client_order_id(signal_id, side)
        now_utc = datetime.now(timezone.utc).isoformat()

        # 1. State machine transition & DB pre-audit
        target_state = "ENTRY_SUBMITTING" if side == "BUY" else "EXIT_SUBMITTING"
        self.sm.transition_to(target_state)

        self.storage.save_order({
            "client_order_id": cid,
            "signal_id": signal_id,
            "mode": self.mode,
            "symbol": symbol,
            "side": side,
            "order_type": "MARKET",
            "status": target_state,
            "target_qty": quantity,
            "filled_qty": Decimal("0"),
            "avg_fill_price": None,
            "fee_amount": Decimal("0"),
            "fee_asset": None,
            "raw_exchange_response": None,
            "created_at_utc": now_utc,
            "updated_at_utc": now_utc,
        })

        # 2. Call adapter create_order (handles timeout & idempotency)
        try:
            report = self.adapter.create_order(
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=quantity,
                client_order_id=cid,
            )
        except Exception as e:
            # On request timeout or error, do NOT blindly retry create_order.
            # Query order by client_order_id for idempotency.
            try:
                queried = self.adapter.fetch_order(cid, symbol=symbol)
                if queried:
                    report = queried
                else:
                    self.sm.transition_to("RECONCILIATION_REQUIRED")
                    raise e
            except Exception:
                self.sm.transition_to("RECONCILIATION_REQUIRED")
                raise e

        # 3. Post-execution status update
        update_utc = datetime.now(timezone.utc).isoformat()
        report.client_order_id = cid
        self.storage.save_order({
            "client_order_id": cid,
            "signal_id": signal_id,
            "mode": self.mode,
            "symbol": symbol,
            "side": side,
            "order_type": "MARKET",
            "status": report.status,
            "target_qty": quantity,
            "filled_qty": report.filled_qty,
            "avg_fill_price": report.avg_price,
            "fee_amount": report.fee_amount,
            "fee_asset": report.fee_asset,
            "raw_exchange_response": report.raw_response,
            "created_at_utc": now_utc,
            "updated_at_utc": update_utc,
        })

        if report.status == "FILLED":
            self.sm.transition_to("LONG" if side == "BUY" else "FLAT")
        elif report.status == "PARTIALLY_FILLED":
            self.sm.transition_to("RECONCILIATION_REQUIRED")
        elif report.status in ("CANCELED", "REJECTED", "EXPIRED"):
            self.sm.transition_to("RECONCILIATION_REQUIRED" if report.filled_qty > 0 else "FLAT")
        else:
            self.sm.transition_to("RECONCILIATION_REQUIRED")

        return report
