"""Read-only tools the strategy agent may call before authoring a policy.

The agent is deliberately not given execution access. Every tool is a bounded
query over state the runtime already stores, and every result is trimmed
before it reaches the model:

* no credentials, no other accounts, no writes;
* prices as strings, indicator values as floats, timestamps as ISO-8601;
* per-call limits keep a model from pulling the whole database into its
  context window.

``Toolbox.call`` returns ``{"summary", "data"}``. The adapter sends ``data``
to the model and keeps ``summary`` for the decision journal, so the UI can
show *what the agent looked at* without replaying full payloads.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import asyncio
from collections import Counter, deque
from datetime import timedelta
from decimal import Decimal
from typing import Any, Sequence

from ..domain.enums import PositionStatus
from ..domain.money import canonical_str
from .features import Candle
from .klines import INDICATOR_NAMES, compute_indicators, validate_interval

MAX_TOOL_CALLS = 16
MAX_CANDLES = 60
DEFAULT_CANDLES = 30
CANDLE_WARMUP = 60

MARKET_OVERVIEW = {
    "name": "get_market_overview",
    "description": (
        "Current mid price, spread and derived features (returns, range, ATR, "
        "moving averages, RSI) for every symbol in the traded universe."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GET_CANDLES = {
    "name": "get_candles",
    "description": (
        "Closed OHLCV candles for one traded symbol, with SMA20, SMA60 and "
        "RSI14 aligned to each bar. Use it to inspect trend, momentum and "
        "recent price action instead of guessing from a single tick."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "e.g. BTCUSDT"},
            "interval": {
                "type": "string",
                "enum": ["1m", "5m", "15m", "1h", "4h"],
                "default": "15m",
            },
            "limit": {
                "type": "integer",
                "minimum": 20,
                "maximum": MAX_CANDLES,
                "default": DEFAULT_CANDLES,
            },
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
}

GET_ACCOUNT_STATE = {
    "name": "get_account_state",
    "description": (
        "Equity, available quote cash, gross exposure, daily realized PnL, "
        "drawdown and the number of open positions."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

GET_OPEN_POSITIONS = {
    "name": "get_open_positions",
    "description": (
        "Every open position with entry, current mark, unrealized PnL, stop "
        "and take-profit distance, age and the venue holding it."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

GET_RECENT_DECISIONS = {
    "name": "get_recent_decisions",
    "description": (
        "Previously authored policies, newest first: regime, chosen universe, "
        "rationale, clamps and expiry. This is the agent's episodic memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}
        },
        "additionalProperties": False,
    },
}

GET_RECENT_TRADES = {
    "name": "get_recent_trades",
    "description": (
        "Closed positions, newest first, with entry, realized PnL and hold "
        "time. Use it to see how recent decisions actually turned out."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
        },
        "additionalProperties": False,
    },
}

GET_RECENT_EVALUATIONS = {
    "name": "get_recent_evaluations",
    "description": (
        "Recent Jev evaluations for the traded symbols: the answer it gave, "
        "how probable it thought that answer, policy fit and quality. Use it "
        "to calibrate jev_gate thresholds — a minimum above what Jev actually "
        "returns blocks every entry, however good the setup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 40,
                "default": 15,
            }
        },
        "additionalProperties": False,
    },
}

GET_PERFORMANCE = {
    "name": "get_performance_summary",
    "description": (
        "Aggregate realized performance across closed positions: trade count, "
        "wins, losses, win rate, net PnL and a per-symbol breakdown."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

GET_PRIOR_STRATEGY = {
    "name": "get_prior_strategy",
    "description": (
        "The policy currently in force and what it produced over the last "
        "N hours: its Jev gate, how many signals it generated, the Jev "
        "choices it received, its policy_fit range and the closed trades "
        "with their PnL. Use it to keep what worked and change what did not."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lookback_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": 168,
                "description": "How far back to summarize outcomes.",
            }
        },
        "additionalProperties": False,
    },
}

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    MARKET_OVERVIEW,
    GET_CANDLES,
    GET_ACCOUNT_STATE,
    GET_OPEN_POSITIONS,
    GET_RECENT_DECISIONS,
    GET_RECENT_EVALUATIONS,
    GET_RECENT_TRADES,
    GET_PERFORMANCE,
    GET_PRIOR_STRATEGY,
)
TOOL_NAMES = frozenset(spec["name"] for spec in TOOL_SPECS)

# Every tool is a read (repo queries, in-memory candles). None of them holds a
# shared mutable buffer, so calls within one model turn can run concurrently;
# a tool that ever needs sequencing must be removed from this set.
PARALLEL_TOOLS = TOOL_NAMES


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _num(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _blocked_reason(signals: list[Any], choices: Counter, closed: list[Any]) -> str:
    """Why the prior policy did or did not trade, in one word for the model.

    ``triggered`` means the gate let something through; the other values say
    which leg held the gate shut, so the next policy knows what to change.
    """

    if closed:
        return "triggered"
    if not signals:
        return "no_signals"
    if set(choices) == {"reject"}:
        return "blocked_by_jev_choice"
    return "blocked_by_thresholds"


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


class AgentToolbox:
    """A per-cycle toolbox bound to one account and one symbol universe."""

    def __init__(
        self,
        *,
        repo,
        market,
        account_id: str,
        mode: str,
        symbols: Sequence[str],
        snapshots: Sequence[Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.repo = repo
        self.market = market
        self.account_id = account_id
        self.mode = mode
        self.symbols = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        self._snapshots = {snapshot.symbol: snapshot for snapshot in (snapshots or [])}
        self._now = now or datetime.now(UTC)
        self._marks_cache: dict[str, Decimal] | None = None
        self.calls = 0
        # Answers from tools that ran in parallel during the current turn.
        # They are handed over in the order the model asked, not the order
        # they happened to finish, so two runs with the same inputs read the
        # same transcript.
        self._inbox: deque[dict[str, Any]] = deque()

    # -- contract --------------------------------------------------------
    def specs(self) -> list[dict[str, Any]]:
        return [dict(spec) for spec in TOOL_SPECS]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown tool {name!r}")
        self.calls += 1
        if self.calls > MAX_TOOL_CALLS:
            raise ValueError("tool call budget exhausted")
        arguments = arguments if isinstance(arguments, dict) else {}
        handler = getattr(self, f"_tool_{name}")
        if self._inbox:
            # This call's answer was already computed alongside its siblings.
            pending = self._inbox.popleft()
            if pending["name"] == name:
                return pending["result"]
            self._inbox.clear()
        return await handler(**arguments)

    def supports_parallel(self, name: str) -> bool:
        return name in PARALLEL_TOOLS

    async def call_parallel(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run read-only tools at once; answers queue for ``call`` in order.

        A failure is captured like ``call`` would report it, so the model sees
        the same content either way.
        """

        if name not in PARALLEL_TOOLS:
            raise ValueError(f"{name!r} cannot run in parallel")
        self.calls += 1
        if self.calls > MAX_TOOL_CALLS:
            raise ValueError("tool call budget exhausted")
        arguments = arguments if isinstance(arguments, dict) else {}
        handler = getattr(self, f"_tool_{name}")
        try:
            result = await handler(**arguments)
        except Exception as exc:  # noqa: BLE001 - reported to the model, as in call()
            result = {
                "summary": f"{type(exc).__name__}: {exc}",
                "data": {"error": str(exc), "tool": name},
            }
        # The "__failed" marker never reaches the model or the journal: only
        # the ok flag does, exactly as on the sequential path.
        if result.pop("__failed", False):
            result["data"]["error"] = str(result["data"].get("error", ""))
        self._inbox.append({"name": name, "result": result})
        return result

    async def memory(self) -> dict[str, Any]:
        """The context pack assembled before the agent starts thinking."""

        memory: dict[str, Any] = {
            "as_of": self._now.isoformat(),
            "mode": self.mode,
        }
        loaders = {
            "recent_decisions": lambda: self._tool_get_recent_decisions(limit=4),
            "recent_evaluations": lambda: self._tool_get_recent_evaluations(limit=10),
            "open_positions": self._tool_get_open_positions,
            "recent_trades": lambda: self._tool_get_recent_trades(limit=6),
            "performance": self._tool_get_performance_summary,
            "prior_strategy": self._tool_get_prior_strategy,
        }
        answers = await asyncio.gather(
            *(loader() for loader in loaders.values()), return_exceptions=True
        )
        for key, answer in zip(loaders, answers, strict=True):
            if isinstance(answer, BaseException):
                memory[key] = {"unavailable": type(answer).__name__}
            else:
                memory[key] = answer["data"]
        return memory

    # -- helpers ---------------------------------------------------------
    def _require_symbol(self, symbol: Any) -> str:
        normalized = str(symbol or "").strip().upper()
        if normalized not in self.symbols:
            raise ValueError(
                f"{normalized or 'symbol'} is outside the traded universe "
                f"({', '.join(self.symbols)})"
            )
        return normalized

    async def _marks(self) -> dict[str, Decimal]:
        if self._marks_cache is None:
            marks = dict(await self.repo.latest_market_marks())
            marks.update({symbol: snap.mid for symbol, snap in self._snapshots.items()})
            self._marks_cache = marks
        return self._marks_cache

    async def _closed_positions(self, limit: int) -> list[Any]:
        rows = await self.repo.list_positions(limit=max(limit * 3, 50), open_only=False)
        closed = [row for row in rows if row.status == PositionStatus.CLOSED.value]
        return closed[:limit]

    # -- tools -----------------------------------------------------------
    async def _tool_get_market_overview(self) -> dict[str, Any]:
        symbols: dict[str, Any] = {}
        for symbol, snapshot in self._snapshots.items():
            symbols[symbol] = {
                "mid": canonical_str(snapshot.mid),
                "bid": canonical_str(snapshot.bid),
                "ask": canonical_str(snapshot.ask),
                "spread_bps": canonical_str(snapshot.spread_bps),
                "features": {key: _num(value) for key, value in snapshot.features.items()},
                "synthetic": snapshot.is_synthetic,
            }
        if not symbols:
            marks = await self._marks()
            symbols = {
                symbol: {"mid": canonical_str(marks[symbol])}
                for symbol in self.symbols
                if symbol in marks
            }
        data = {
            "universe": self.symbols,
            "as_of": self._now.isoformat(),
            "data_source": getattr(self.market, "source", "unknown"),
            "symbols": symbols,
        }
        return {
            "summary": f"{len(symbols)} symbols: {', '.join(sorted(symbols)) or 'no quotes'}",
            "data": data,
        }

    async def _tool_get_candles(
        self, symbol: str, interval: str = "15m", limit: int = DEFAULT_CANDLES
    ) -> dict[str, Any]:
        normalized = self._require_symbol(symbol)
        try:
            interval = validate_interval(str(interval or "15m"))
        except ValueError:
            interval = "15m"
        count = _clamp_int(limit, DEFAULT_CANDLES, 20, MAX_CANDLES)
        candles: list[Candle] = await self.market.klines(
            normalized, interval=interval, limit=count, warmup=CANDLE_WARMUP
        )
        if not candles:
            raise ValueError(f"no closed candles returned for {normalized} {interval}")
        values = compute_indicators(candles, INDICATOR_NAMES)
        visible = candles[-count:]
        # Only the latest value of each indicator: the full aligned series
        # costs several thousand tokens per call and is re-sent every turn.
        aligned = {
            name: _num(
                next((value for value in reversed(series) if value is not None), None)
            )
            for name, series in values.items()
        }
        last = visible[-1]
        sma20 = aligned["sma20"]
        rsi14 = aligned["rsi14"]
        data = {
            "symbol": normalized,
            "interval": interval,
            "bars": [
                {
                    "t": _iso(candle.close_time),
                    "o": _num(candle.open),
                    "h": _num(candle.high),
                    "l": _num(candle.low),
                    "c": _num(candle.close),
                    "v": _num(candle.volume, 2),
                }
                for candle in visible
            ],
            "indicators": aligned,
        }
        summary = (
            f"{normalized} {interval}: {len(visible)} closed bars, last {last.close:.4f}, "
            f"SMA20 {sma20 if sma20 is not None else 'n/a'}, "
            f"RSI14 {rsi14 if rsi14 is not None else 'n/a'}"
        )
        return {"summary": summary, "data": data}

    async def _tool_get_account_state(self) -> dict[str, Any]:
        marks = await self._marks()
        account = await self.repo.account_snapshot(self.account_id, marks, self._now)
        data = {
            "equity_usdt": canonical_str(account.equity_usdt),
            "available_quote_usdt": canonical_str(account.available_quote_usdt),
            "gross_exposure_usdt": canonical_str(account.gross_exposure_usdt),
            "open_positions": account.open_positions,
            "daily_realized_pnl_usdt": canonical_str(account.daily_realized_pnl_usdt),
            "daily_realized_pnl_pct": canonical_str(account.daily_realized_pnl_pct),
            "drawdown_pct": canonical_str(account.drawdown_pct),
            "mode": self.mode,
        }
        summary = (
            f"equity {data['equity_usdt']} USDT, cash {data['available_quote_usdt']}, "
            f"{account.open_positions} open, daily PnL {data['daily_realized_pnl_pct']}%"
        )
        return {"summary": summary, "data": data}

    async def _tool_get_open_positions(self) -> dict[str, Any]:
        positions = await self.repo.get_open_positions(self.account_id)
        marks = await self._marks()
        items: list[dict[str, Any]] = []
        total_unrealized = Decimal("0")
        for position in positions:
            mark = marks.get(position.symbol, position.avg_entry_price)
            unrealized = position.qty * (mark - position.avg_entry_price)
            total_unrealized += unrealized
            age = max(0, int((self._now - position.opened_at).total_seconds()))
            items.append(
                {
                    "id": position.id,
                    "symbol": position.symbol,
                    "qty": canonical_str(position.qty),
                    "avg_entry_price": canonical_str(position.avg_entry_price),
                    "mark_price": canonical_str(mark),
                    "unrealized_pnl_usdt": canonical_str(unrealized),
                    "stop_loss_bps": canonical_str(position.stop_loss_bps),
                    "take_profit_bps": canonical_str(position.take_profit_bps),
                    "age_seconds": age,
                    "max_holding_seconds": position.max_holding_seconds,
                    "mode": position.mode,
                    "is_simulated": position.is_simulated,
                    "exit_requested": position.exit_requested_at is not None,
                }
            )
        summary = (
            f"{len(items)} open position(s), unrealized {canonical_str(total_unrealized)} USDT"
            + (f": {', '.join(item['symbol'] for item in items)}" if items else "")
        )
        return {"summary": summary, "data": {"positions": items}}

    async def _tool_get_recent_decisions(self, limit: int = 5) -> dict[str, Any]:
        count = _clamp_int(limit, 5, 1, 20)
        rows = await self.repo.list_policies(limit=count)
        items: list[dict[str, Any]] = []
        for row in rows:
            raw = row.raw_json or {}
            effective = row.effective_json or {}
            regime = effective.get("regime") or {}
            universe = effective.get("universe") or []
            items.append(
                {
                    "policy_id": row.policy_id,
                    "policy_hash": row.policy_hash[:20],
                    "status": row.status,
                    "source": row.source,
                    "simulated": row.is_simulated,
                    "generated_at": _iso(row.generated_at),
                    "expires_at": _iso(row.expires_at),
                    "regime": regime.get("label"),
                    "regime_confidence": regime.get("confidence"),
                    "symbols": [
                        entry.get("symbol")
                        for entry in universe
                        if isinstance(entry, dict) and entry.get("symbol")
                    ],
                    "rationale": (raw.get("rationale") or "")[:800],
                    "clamped_fields": [
                        clamp.get("field")
                        for clamp in (row.clamps_json or [])
                        if isinstance(clamp, dict)
                    ],
                }
            )
        latest = items[0] if items else None
        summary = (
            f"{len(items)} recent decision(s)"
            + (f"; latest {latest['regime']} on {', '.join(latest['symbols'])}" if latest else "")
        )
        return {"summary": summary, "data": {"decisions": items}}

    async def _tool_get_recent_trades(self, limit: int = 10) -> dict[str, Any]:
        count = _clamp_int(limit, 10, 1, 50)
        rows = await self._closed_positions(count)
        items: list[dict[str, Any]] = []
        net = Decimal("0")
        wins = 0
        for row in rows:
            net += row.realized_pnl_usdt
            if row.realized_pnl_usdt > 0:
                wins += 1
            duration = (
                int((row.closed_at - row.opened_at).total_seconds())
                if row.closed_at is not None
                else None
            )
            items.append(
                {
                    "position_id": row.id,
                    "symbol": row.symbol,
                    "qty": canonical_str(row.qty),
                    "avg_entry_price": canonical_str(row.avg_entry_price),
                    "realized_pnl_usdt": canonical_str(row.realized_pnl_usdt),
                    "opened_at": _iso(row.opened_at),
                    "closed_at": _iso(row.closed_at),
                    "held_seconds": duration,
                    "policy_id": row.policy_id,
                    "mode": row.mode,
                }
            )
        summary = (
            f"{len(items)} closed trade(s), net {canonical_str(net)} USDT, "
            f"{wins} winning"
        )
        return {"summary": summary, "data": {"trades": items}}

    async def _tool_get_recent_evaluations(
        self, limit: int = 15
    ) -> dict[str, Any]:
        count = _clamp_int(limit, 15, 1, 40)
        rows = await self.repo.list_signals(limit=max(count * 3, 60))
        evaluations: list[dict[str, Any]] = []
        for row in rows:
            score = row.score_json or {}
            if not isinstance(score, dict) or "entry_choice" not in score:
                continue
            evaluations.append(
                {
                    "symbol": row.symbol,
                    "at": _iso(row.created_at),
                    "state": row.state,
                    "choice": score.get("entry_choice"),
                    "probability": _num(score.get("entry_probability")),
                    "policy_fit": _num(score.get("policy_fit")),
                    "quality_score": _num(score.get("quality_score")),
                    "simulated": bool(score.get("simulated")),
                }
            )
            if len(evaluations) >= count:
                break
        if not evaluations:
            return {
                "summary": "no Jev evaluations recorded yet",
                "data": {"evaluations": [], "choices": {}, "best": None},
            }
        choices = Counter(str(item["choice"]) for item in evaluations)
        qualities = [
            item["quality_score"]
            for item in evaluations
            if item["quality_score"] is not None
        ]
        fits = [
            item["policy_fit"]
            for item in evaluations
            if item["policy_fit"] is not None
        ]
        best = {
            "quality_score": max(qualities) if qualities else None,
            "policy_fit": max(fits) if fits else None,
        }
        counts = ", ".join(f"{name} {n}" for name, n in choices.most_common())
        summary = (
            f"{len(evaluations)} recent evaluations: {counts}; "
            f"best quality {best['quality_score']}, best fit {best['policy_fit']}"
        )
        return {
            "summary": summary,
            "data": {
                "evaluations": evaluations,
                "choices": dict(choices),
                "best": best,
            },
        }

    async def _tool_get_prior_strategy(
        self, lookback_hours: int = 24
    ) -> dict[str, Any]:
        """The previous policy and what it actually produced, side by side.

        Without this every run starts from a blank sheet and re-learns the
        same lessons; mature agents (AutoGen's conversable-group memory,
        OpenHands' condenser) always carry the last outcome forward.
        """

        hours = _clamp_int(lookback_hours, 24, 1, 168)
        rows = await self.repo.list_policies(limit=1)
        if not rows:
            return {
                "summary": "no prior policy",
                "data": {"prior": None},
            }
        row = rows[0]
        raw = row.raw_json if isinstance(row.raw_json, dict) else {}
        since = self._now - timedelta(hours=hours)
        signals = [
            signal
            for signal in await self.repo.list_signals(limit=600)
            if signal.policy_id == row.id and signal.created_at >= since
        ]
        closed = [
            position
            for position in await self._closed_positions(200)
            if position.policy_id == row.id
        ]
        right = [position for position in closed if position.realized_pnl_usdt > 0]
        fits = [
            float(score["policy_fit"])
            for score in (signal.score_json or {} for signal in signals)
            if isinstance(score, dict) and score.get("policy_fit") is not None
        ]
        choices = Counter(
            str((signal.score_json or {}).get("entry_choice"))
            for signal in signals
            if isinstance(signal.score_json, dict) and signal.score_json.get("entry_choice")
        )
        prior = {
            "policy_id": row.id,
            "compiled_at": _iso(row.compiled_at),
            "expires_at": _iso(row.expires_at),
            "jev_gate": raw.get("jev_gate"),
            "universe": [
                item.get("symbol") for item in raw.get("universe") or [] if isinstance(item, dict)
            ],
            "rationale": raw.get("rationale"),
        }
        outcome = {
            "hours": hours,
            "signals": len(signals),
            "jev_choices": dict(choices),
            "policy_fit_min": round(min(fits), 3) if fits else None,
            "policy_fit_max": round(max(fits), 3) if fits else None,
            "closed": len(closed),
            "wins": len(right),
            "realized_pnl_usdt": canonical_str(
                sum((position.realized_pnl_usdt for position in closed), Decimal("0"))
            ),
            "gate_state": _blocked_reason(signals, choices, closed),
        }
        gate = prior["jev_gate"] or {}
        summary = (
            f"prior policy {str(row.id)[:8]}: gate {gate.get('entry_choice')} "
            f"/prob {gate.get('min_choice_probability')} /fit {gate.get('min_policy_fit')} "
            f"/q {gate.get('min_quality_score')}; {outcome['signals']} signals in "
            f"{hours}h ({outcome['gate_state']}), {outcome['closed']} closed, "
            f"{outcome['wins']} wins"
        )
        return {"summary": summary, "data": {"prior": prior, "outcome": outcome}}

    async def _tool_get_performance_summary(self) -> dict[str, Any]:
        rows = await self._closed_positions(200)
        wins = [row for row in rows if row.realized_pnl_usdt > 0]
        losses = [row for row in rows if row.realized_pnl_usdt < 0]
        net = sum((row.realized_pnl_usdt for row in rows), Decimal("0"))
        decided = len(wins) + len(losses)
        by_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = by_symbol.setdefault(
                row.symbol, {"trades": 0, "net_pnl_usdt": Decimal("0")}
            )
            bucket["trades"] += 1
            bucket["net_pnl_usdt"] += row.realized_pnl_usdt
        data = {
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / decided * 100, 2) if decided else 0.0,
            "net_pnl_usdt": canonical_str(net),
            "average_pnl_usdt": canonical_str(net / len(rows)) if rows else "0",
            "by_symbol": [
                {
                    "symbol": symbol,
                    "trades": bucket["trades"],
                    "net_pnl_usdt": canonical_str(bucket["net_pnl_usdt"]),
                }
                for symbol, bucket in sorted(
                    by_symbol.items(),
                    key=lambda item: item[1]["net_pnl_usdt"],
                    reverse=True,
                )
            ],
        }
        summary = (
            f"{len(rows)} closed trades, net {canonical_str(net)} USDT, "
            f"win rate {data['win_rate_pct']}%"
        )
        return {"summary": summary, "data": data}
