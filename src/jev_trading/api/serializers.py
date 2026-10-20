"""Row -> JSON projections used by the control plane.

Money and quantity values always leave the process as canonical decimal
strings; the frontend never receives a binary float for a financial value.
"""

from __future__ import annotations

from typing import Any

from ..domain.money import canonical_str
from ..storage.models import (
    AgentRunRow,
    AuditEventRow,
    BacktestRunRow,
    FillRow,
    JevEvaluationRow,
    OrderRow,
    PnlSnapshotRow,
    PositionRow,
    RiskDecisionRow,
    SignalEventRow,
    SignalRow,
    StrategyPolicyRow,
    TradeIntentRow,
)


def _money(value: Any) -> str | None:
    return None if value is None else canonical_str(value)


def signal(row: SignalRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "state": row.state,
        "side": row.side,
        "policy_id": row.policy_id,
        "snapshot_id": row.snapshot_id,
        "jev_evaluation_id": row.jev_evaluation_id,
        "score": row.score_json,
        "reason": row.reason,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def signal_event(row: SignalEventRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "signal_id": row.signal_id,
        "state": row.state,
        "detail": row.detail_json,
        "created_at": row.created_at.isoformat(),
    }


def jev_evaluation(row: JevEvaluationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "signal_id": row.signal_id,
        "model": row.model,
        "answers": row.answers_json,
        "latency_ms": row.latency_ms,
        "error_code": row.error_code,
        "request": row.request_json,
        "response": row.response_json,
        "created_at": row.created_at.isoformat(),
    }


def intent(row: TradeIntentRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "signal_id": row.signal_id,
        "policy_id": row.policy_id,
        "mode": row.mode,
        "symbol": row.symbol,
        "side": row.side,
        "intent_type": row.intent_type,
        "order_preference": row.order_preference,
        "quote_notional": _money(row.quote_notional),
        "max_slippage_bps": _money(row.max_slippage_bps),
        "limit_price": _money(row.limit_price),
        "stop_loss_bps": _money(row.stop_loss_bps),
        "take_profit_bps": _money(row.take_profit_bps),
        "max_holding_seconds": row.max_holding_seconds,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "risk_snapshot_hash": row.risk_snapshot_hash,
        "provenance": row.provenance_json,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }


def risk_decision(row: RiskDecisionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "intent_id": row.intent_id,
        "approved": row.approved,
        "failed_rules": row.failed_rules_json,
        "effective": row.effective_json,
        "snapshot_hash": row.snapshot_hash,
        "decision_hash": row.decision_hash,
        "approved_intent": row.approved_intent_json,
        "created_at": row.created_at.isoformat(),
    }


def order(row: OrderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "intent_id": row.intent_id,
        "exchange_order_id": row.exchange_order_id,
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "qty": _money(row.qty),
        "price": _money(row.price),
        "status": row.status,
        "filled_qty": _money(row.filled_qty),
        "avg_fill_price": _money(row.avg_fill_price),
        "fee_quote": _money(row.fee_quote),
        "mode": row.mode,
        "is_simulated": row.is_simulated,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def fill(row: FillRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "order_id": row.order_id,
        "qty": _money(row.qty),
        "price": _money(row.price),
        "fee_quote": _money(row.fee_quote),
        "fee_asset": row.fee_asset,
        "created_at": row.created_at.isoformat(),
    }


def position(row: PositionRow, mark: Any | None = None) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "account_id": row.account_id,
        "symbol": row.symbol,
        "qty": _money(row.qty),
        "avg_entry_price": _money(row.avg_entry_price),
        "stop_loss_bps": _money(row.stop_loss_bps),
        "take_profit_bps": _money(row.take_profit_bps),
        "max_holding_seconds": row.max_holding_seconds,
        "status": row.status,
        "realized_pnl_usdt": _money(row.realized_pnl_usdt),
        "opened_at": row.opened_at.isoformat(),
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "policy_id": row.policy_id,
        "signal_id": row.signal_id,
        "is_simulated": row.is_simulated,
    }
    if mark is not None:
        payload["mark_price"] = _money(mark)
        payload["unrealized_pnl_usdt"] = _money(row.qty * (mark - row.avg_entry_price))
    return payload


def pnl_snapshot(row: PnlSnapshotRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "as_of": row.as_of.isoformat(),
        "equity_usdt": _money(row.equity_usdt),
        "cash_quote_usdt": _money(row.cash_quote_usdt),
        "realized_pnl_usdt": _money(row.realized_pnl_usdt),
        "unrealized_pnl_usdt": _money(row.unrealized_pnl_usdt),
        "gross_exposure_usdt": _money(row.gross_exposure_usdt),
        "daily_realized_pnl_usdt": _money(row.daily_realized_pnl_usdt),
        "drawdown_pct": _money(row.drawdown_pct),
    }


def policy(row: StrategyPolicyRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "policy_id": row.policy_id,
        "status": row.status,
        "source": row.source,
        "is_simulated": row.is_simulated,
        "mode_scope": row.mode_scope_json,
        "clamps": row.clamps_json,
        "policy_hash": row.policy_hash,
        "raw": row.raw_json,
        "effective": row.effective_json,
        "generated_at": row.generated_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }


def audit_event(row: AuditEventRow) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "id": row.id,
        "event_type": row.event_type,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "payload": row.payload_json,
        "prev_hash": row.prev_hash,
        "event_hash": row.event_hash,
        "created_at": row.created_at.isoformat(),
    }


def backtest(row: BacktestRunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "request": row.request_json,
        "result": row.result_json,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def agent_run(row: AgentRunRow, *, include_tools: bool = False) -> dict[str, Any]:
    """One strategy-agent attempt. Tool traces are opt-in (they can be large)."""

    payload: dict[str, Any] = {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "policy_id": row.policy_id,
        "mode": row.mode,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "turns": row.turns,
        "latency_ms": row.latency_ms,
        "rationale": row.rationale,
        "error_code": row.error_code,
        "tool_count": len(row.tool_calls_json or []),
        "created_at": row.created_at.isoformat(),
    }
    if include_tools:
        payload["tools"] = row.tool_calls_json or []
        payload["usage"] = row.usage_json or {}
    return payload
