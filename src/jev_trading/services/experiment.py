"""Offline A/B/C comparison over real candles and recorded Jev decisions.

Three groups, one replay engine, one cost model:

- ``hold_cash``: never trade (0% return by construction, the zero-trade
  baseline every other group has to beat);
- ``rules``: a deterministic SMA20/SMA60 hourly crossover, no model involved;
- ``jev_all_buys``: every recorded Jev BUY, including the ones the gate blocked;
- ``jev_gate_passed``: only the BUYs the gate admitted.

The point is not to crown a winner on three days of data. It is to put the
same question to all three groups with the same fills, fees and
one-position-at-a-time constraint. Two differences remain and are reported
rather than hidden: the Jev groups use each signal's own recorded policy,
while the baseline uses the policy that was live for that symbol at that
moment (and counts how often it had to assume defaults), and the baseline's
entry is a bar close rather than a recorded ask.

Honesty rules this module enforces:

- missing or lagging data is reported as ``skipped``, never filled in;
- signals whose holding window has not finished are not evaluated;
- the blocked set is summarized per signal (no overlap filter), because the
  question there is "what would each one have done", not "how would a
  portfolio of them have looked".

Run inside the deployed service, where the database and market feed live::

    python -m jev_trading.experiment --days 3

"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..domain.costs import CostModel
from ..domain.market import Candle
from ..domain.money import ZERO, canonical_str
from ..domain.policy import EffectivePolicy, ExitDefaults
from ..storage.db import Database
from ..storage.repository import Repository
from .counterfactual import Replay, simulate
from .features import aggregate, sma
from .klines import fetch_minute_range
from ..walk_forward import load_samples as load_walk_forward_samples
from .market_service import MarketService
from .walk_forward import DEFAULT_MIN_TRADES as DEFAULT_WF_MIN_TRADES
from .walk_forward import build_report as build_walk_forward_report
from .walk_forward import format_report as format_walk_forward

DEFAULT_EXITS = ExitDefaults(
    stop_loss_bps=Decimal("40"),
    take_profit_bps=Decimal("60"),
    max_holding_seconds=5400,
)
#: Fewest samples per fold segment before a walk-forward verdict is more
#: than a description of the procedure.
DEFAULT_WF_MIN_TRADES = 10


@dataclass(frozen=True)
class ExperimentSpec:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    days: int = 3
    end: datetime | None = None
    baseline_fast: int = 20
    baseline_slow: int = 60
    # A default only; the deployed account's size is not a repo fact.
    starting_equity_usdt: Decimal = Decimal("100")

    @property
    def finished_at(self) -> datetime:
        return self.end or datetime.now(UTC)

    @property
    def started_at(self) -> datetime:
        return self.finished_at - timedelta(days=self.days)


@dataclass(frozen=True)
class TradeResult:
    group: str
    symbol: str
    signal_id: str | None
    decided_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    exit_time: datetime
    net_bps: Decimal
    pnl_usdt: Decimal
    entry_probability: float | None = None
    gate: str = "n/a"
    blocked_rule: str | None = None
    mfe_bps: float = 0.0
    mae_bps: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "signal_id": self.signal_id,
            "decided_at": self.decided_at.isoformat(),
            "entry_price": canonical_str(self.entry_price),
            "exit_price": canonical_str(self.exit_price),
            "exit_reason": self.exit_reason,
            "exit_at": self.exit_time.isoformat(),
            "net_bps": round(float(self.net_bps), 2),
            "pnl_usdt": canonical_str(self.pnl_usdt),
            "entry_probability": self.entry_probability,
            "gate": self.gate,
            "blocked_rule": self.blocked_rule,
            "mfe_bps": self.mfe_bps,
            "mae_bps": self.mae_bps,
        }


@dataclass
class GroupReport:
    name: str
    description: str
    trades: list[TradeResult] = field(default_factory=list)
    skipped_overlap: int = 0
    skipped_no_data: int = 0
    candidates: int = 0


def summarize(trades: Sequence[TradeResult], starting_equity: Decimal) -> dict[str, Any]:
    """Net-of-cost performance for one group."""

    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    wins = [trade for trade in ordered if trade.pnl_usdt > 0]
    losses = [trade for trade in ordered if trade.pnl_usdt < 0]
    net = sum((trade.pnl_usdt for trade in ordered), ZERO)
    gross_win = sum((trade.pnl_usdt for trade in wins), ZERO)
    gross_loss = sum((trade.pnl_usdt for trade in losses), ZERO)
    equity = starting_equity
    peak = starting_equity
    max_drawdown = ZERO
    for trade in ordered:
        equity += trade.pnl_usdt
        peak = max(peak, equity)
        if peak > ZERO:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * Decimal("100"))
    reasons: dict[str, int] = {}
    for trade in ordered:
        reasons[trade.exit_reason] = reasons.get(trade.exit_reason, 0) + 1
    average_bps = (
        sum((trade.net_bps for trade in ordered), ZERO) / len(ordered) if ordered else ZERO
    )
    return {
        "trades": len(ordered),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(ordered) * 100, 2) if ordered else None,
        "net_pnl_usdt": canonical_str(net),
        "return_pct": round(float(net / starting_equity * 100), 3)
        if starting_equity > ZERO
        else None,
        "average_net_bps": round(float(average_bps), 2),
        "profit_factor": round(float(gross_win / abs(gross_loss)), 3)
        if gross_loss < ZERO
        else None,
        "max_drawdown_pct": round(float(max_drawdown), 3),
        "exit_reasons": dict(sorted(reasons.items())),
        "trades_detail": [trade.as_dict() for trade in ordered],
    }


def summarize_signal_level(
    rows: Sequence[dict[str, Any]], starting_equity: Decimal
) -> dict[str, Any]:
    """Per-signal results: each BUY judged alone, gate status kept apart.

    The blocked rows here are not independent: several often land inside one
    few-minute burst. The spread across them says how noisy the estimate is,
    not how confident we may be in its sign.
    """

    def stats(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        nets = [Decimal(str(item["net_bps"])) for item in subset]
        wins = sum(1 for value in nets if value > 0)
        average = sum(nets, ZERO) / len(nets) if nets else ZERO
        spread = (
            (sum((value - average) ** 2 for value in nets) / len(nets)).sqrt() if nets else ZERO
        )
        return {
            "signals": len(subset),
            "wins": wins,
            "win_rate_pct": round(wins / len(subset) * 100, 2) if subset else None,
            "average_net_bps": round(float(average), 2),
            "stdev_net_bps": round(float(spread), 2),
        }

    def timeout_path(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        timeouts = [item for item in subset if item["exit_reason"] == "timeout"]
        if not timeouts:
            return {"timeouts": 0}
        mfe = [item["mfe_bps"] for item in timeouts]
        mae = [item["mae_bps"] for item in timeouts]
        # Did the path ever reach a useful profit before giving it back?
        near_target = sum(1 for value in mfe if value >= 40.0)
        return {
            "timeouts": len(timeouts),
            "average_mfe_bps": round(sum(mfe) / len(mfe), 2),
            "average_mae_bps": round(sum(mae) / len(mae), 2),
            "timeouts_that_peaked_above_40_bps": near_target,
            "largest_mfe_bps": round(max(mfe), 2),
        }

    blocked = [item for item in rows if item["gate"] == "blocked"]
    passed = [item for item in rows if item["gate"] == "passed"]
    other = [item for item in rows if item["gate"] == "other"]
    return {
        "all": stats(rows),
        "gate_passed": stats(passed),
        "gate_blocked": stats(blocked),
        "gate_other": stats(other),
        "timeout_path_all": timeout_path(rows),
        "timeout_path_gate_passed": timeout_path(passed),
        "timeout_path_gate_blocked": timeout_path(blocked),
        "timeout_path_gate_other": timeout_path(other),
    }


class ExperimentRunner:
    def __init__(self, *, settings, repo: Repository, market: MarketService) -> None:
        self.settings = settings
        self.cost = CostModel.from_settings(settings)
        self.repo = repo
        self.market = market

    # -- data ------------------------------------------------------------
    async def _minutes(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        return await fetch_minute_range(self.market, symbol, start, end)

    # -- replay ----------------------------------------------------------
    def _notional(self, policy: EffectivePolicy | None, symbol: str) -> Decimal:
        return min(
            self.settings.max_position_notional_usdt,
            policy.symbol_policy(symbol).max_position_notional_usdt
            if policy is not None and policy.symbol_policy(symbol) is not None
            else self.settings.max_position_notional_usdt,
        )

    def _replay(
        self,
        *,
        symbol: str,
        bars: Sequence[Candle],
        entered_at: datetime,
        entry_price: Decimal,
        exits: ExitDefaults,
        notional: Decimal,
        signal_id: str | None,
        gate: str,
        probability: float | None,
        blocked_rule: str | None,
        group: str,
    ) -> TradeResult | None:
        horizon_end = entered_at + timedelta(seconds=exits.max_holding_seconds)
        window = [
            bar for bar in bars if bar.open_time > entered_at and bar.close_time <= horizon_end
        ]
        replay: Replay | None = simulate(
            candles=window,
            entry_price=entry_price,
            exit_rules=exits,
            horizon_end=horizon_end,
            taker_fee_bps=self.cost.fee_bps,
            exit_slippage_bps=self.cost.exit_slippage_bps,
            notional=notional,
        )
        if replay is None or replay.exit_time is None:
            return None
        return TradeResult(
            group=group,
            symbol=symbol,
            signal_id=signal_id,
            decided_at=entered_at,
            entry_price=replay.entry_price,
            exit_price=replay.exit_price,
            exit_reason=replay.exit_reason,
            exit_time=replay.exit_time,
            net_bps=replay.net_bps,
            pnl_usdt=replay.pnl_usdt,
            entry_probability=probability,
            gate=gate,
            blocked_rule=blocked_rule,
            mfe_bps=round(float(replay.mfe_bps), 2),
            mae_bps=round(float(replay.mae_bps), 2),
        )

    # -- main ------------------------------------------------------------
    async def run(self, spec: ExperimentSpec) -> dict[str, Any]:
        end = spec.finished_at
        start = spec.started_at
        warmup = timedelta(hours=spec.baseline_slow + 2)
        bars: dict[str, list[Candle]] = {}
        for symbol in spec.symbols:
            bars[symbol] = await self._minutes(symbol, start - warmup, end)

        rows = await self.repo.list_signals(limit=10000)
        signals = [row for row in rows if start <= row.created_at <= end and row.score_json]
        buys = [row for row in signals if str(row.score_json.get("entry_choice")) == "buy"]

        policies: dict[str, EffectivePolicy] = {}

        async def policy_for(policy_id: str) -> EffectivePolicy | None:
            if policy_id not in policies:
                stored = await self.repo.get_policy_by_policy_id(policy_id)
                if stored is None:
                    return None
                policies[policy_id] = EffectivePolicy.model_validate(stored.effective_json)
            return policies[policy_id]

        groups = {
            "jev_all_buys": GroupReport(
                "jev_all_buys", "every recorded Jev BUY, gate ignored", candidates=len(buys)
            ),
            "jev_gate_passed": GroupReport(
                "jev_gate_passed", "BUYs the gate admitted", candidates=0
            ),
        }
        blocked: list[TradeResult] = []
        # Signal level: each BUY replayed on its own, gate ignored. This is a
        # different question from the portfolio-level groups above, where the
        # one-position rule skips signals that arrive while a trade is open.
        signal_level_rows: list[dict[str, Any]] = []
        skipped_no_data = 0
        skipped_young = 0

        async def replay_signal(row, group: GroupReport) -> TradeResult | None:
            nonlocal skipped_no_data, skipped_young
            policy = await policy_for(row.policy_id)
            if policy is None:
                skipped_no_data += 1
                return None
            snapshot = await self.repo.get_market_snapshot(row.snapshot_id)
            symbol_bars = bars.get(row.symbol, [])
            if snapshot is None or not symbol_bars:
                skipped_no_data += 1
                return None
            horizon_end = row.created_at + timedelta(
                seconds=policy.exit_defaults.max_holding_seconds
            )
            if horizon_end > end:
                skipped_young += 1
                return None
            entry_price = self.cost.entry_price(snapshot.ask)
            trade = self._replay(
                symbol=row.symbol,
                bars=symbol_bars,
                entered_at=row.created_at,
                entry_price=entry_price,
                exits=policy.exit_defaults,
                notional=self._notional(policy, row.symbol),
                signal_id=row.id,
                gate=_gate_bucket(row.state),
                probability=float(row.score_json.get("entry_probability") or 0.0),
                blocked_rule=None,
                group=group.name,
            )
            return trade

        busy_until: dict[str, datetime] = {}
        for row in sorted(buys, key=lambda item: item.created_at):
            gate_passed = row.state != "REJECTED"
            if gate_passed:
                groups["jev_gate_passed"].candidates += 1
            target_groups = [groups["jev_all_buys"]]
            if gate_passed:
                target_groups.append(groups["jev_gate_passed"])
            for group in target_groups:
                if row.created_at < busy_until.get(group.name, datetime.min.replace(tzinfo=UTC)):
                    group.skipped_overlap += 1
                    continue
                trade = await replay_signal(row, group)
                if trade is None:
                    continue
                group.trades.append(trade)
                busy_until[group.name] = trade.exit_time
        # Signal level, second pass: every BUY judged on its own, with no
        # one-position rule. This is what the blocked set means, and it is the
        # only view in which Jev's selection can be compared like for like --
        # the portfolio groups above skip whatever arrives mid-trade.
        for row in sorted(buys, key=lambda item: item.created_at):
            trade = await replay_signal(row, groups["jev_all_buys"])
            if trade is None:
                continue
            signal_level_rows.append(trade.as_dict())
            if row.state == "REJECTED":
                blocked.append(trade)

        baseline = await self._baseline(spec, start, end, bars)
        return {
            "window": {
                "started_at": start.isoformat(),
                "finished_at": end.isoformat(),
                "days": spec.days,
            },
            "data": {
                "signals": len(signals),
                "buy_signals": len(buys),
                "gate_passed_buys": groups["jev_gate_passed"].candidates,
                "blocked_buys": len(buys) - groups["jev_gate_passed"].candidates,
                "minute_bars": {symbol: len(series) for symbol, series in bars.items()},
                "skipped_no_data": skipped_no_data,
                "skipped_window_open": skipped_young,
            },
            "assumptions": [
                "entry pays the recorded ask plus configured slippage; baseline pays the 1h close plus slippage",
                "exits use each policy's own stop/target/holding window, at 1m resolution",
                "one position at a time per group; a signal arriving while a trade is open is skipped",
                "10 bps taker fee per side and 8 bps exit slippage; a bar touching both levels counts as a stop",
                "the blocked set ignores the one-position rule and is reported per signal",
            ],
            "groups": {
                name: {
                    "description": group.description,
                    "candidates": group.candidates,
                    "skipped_overlap": group.skipped_overlap,
                    **summarize(group.trades, spec.starting_equity_usdt),
                }
                for name, group in groups.items()
            },
            "hold_cash": {
                "description": "never trade",
                **summarize([], spec.starting_equity_usdt),
            },
            "rules_baseline": {
                "description": f"SMA{spec.baseline_fast}/{spec.baseline_slow} hourly crossover",
                **baseline,
            },
            "blocked_buys": {
                "description": (
                    "blocked BUYs, each replayed on its own (same rows as "
                    "signal_level.gate_blocked)"
                ),
                **summarize(blocked, spec.starting_equity_usdt),
            },
            "signal_level": {
                "description": (
                    "every BUY replayed on its own, gate ignored: measures Jev's "
                    "selection, not a portfolio (no one-position rule)"
                ),
                **summarize_signal_level(signal_level_rows, spec.starting_equity_usdt),
            },
        }

    async def walk_forward(
        self,
        *,
        days: int = 28,
        fold_count: int = 4,
        min_trades: int = DEFAULT_WF_MIN_TRADES,
        symbols: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Walk-forward over the same recorded outcomes this report replays.

        Shares the loader and the cost model, so a fold and the per-signal
        section above agree about what a candidate returned. Nothing is
        written: the result is a document for a person to act on.
        """

        end = datetime.now(UTC)
        start = end - timedelta(days=max(1, days))
        samples = await load_walk_forward_samples(
            self.repo, start=start, end=end, symbols=tuple(symbols or ())
        )
        horizon = timedelta(
            minutes=max((s.window_minutes for s in samples), default=60)
        )
        return build_walk_forward_report(
            samples,
            start=start,
            end=end,
            horizon=horizon,
            cost=self.cost,
            fold_count=max(1, fold_count),
            min_trades=max(2, min_trades),
        )

    async def _baseline(
        self,
        spec: ExperimentSpec,
        start: datetime,
        end: datetime,
        bars: dict[str, list[Candle]],
    ) -> dict[str, Any]:
        trades: list[TradeResult] = []
        skipped_overlap = 0
        busy_until = datetime.min.replace(tzinfo=UTC)
        policies = await self.repo.list_policies(limit=500)
        defaulted = 0

        def exits_at(moment: datetime, symbol: str) -> ExitDefaults:
            """The exits the live policy carried for *this* symbol.

            A policy that does not list the symbol cannot speak for it; the
            first matching policy in a multi-symbol universe would otherwise
            lend one symbol's stops to another. Falling back to the defaults
            is counted so the report can say how much of the baseline ran on
            assumed exits rather than recorded ones.
            """

            nonlocal defaulted
            for policy in policies:
                if policy.generated_at <= moment <= policy.expires_at:
                    effective = EffectivePolicy.model_validate(policy.effective_json)
                    if effective.symbol_policy(symbol) is not None:
                        return effective.exit_defaults
            defaulted += 1
            return DEFAULT_EXITS

        for symbol in spec.symbols:
            hourly = aggregate(bars[symbol], 60)
            closes = [bar.close for bar in hourly]
            for index in range(spec.baseline_slow, len(hourly)):
                if hourly[index].close_time < start or hourly[index].close_time > end:
                    continue
                fast_now = sma(closes[: index + 1], spec.baseline_fast)
                slow_now = sma(closes[: index + 1], spec.baseline_slow)
                fast_prev = sma(closes[:index], spec.baseline_fast)
                slow_prev = sma(closes[:index], spec.baseline_slow)
                if None in (fast_now, slow_now, fast_prev, slow_prev):
                    continue
                if not (fast_prev <= slow_prev and fast_now > slow_now):
                    continue
                entered_at = hourly[index].close_time
                if entered_at < busy_until:
                    skipped_overlap += 1
                    continue
                exits = exits_at(entered_at, symbol)
                entry_price = self.cost.entry_price(
                    Decimal(str(hourly[index].close))
                )
                trade = self._replay(
                    symbol=symbol,
                    bars=bars[symbol],
                    entered_at=entered_at,
                    entry_price=entry_price,
                    exits=exits,
                    notional=self.settings.max_position_notional_usdt,
                    signal_id=None,
                    gate="n/a",
                    probability=None,
                    blocked_rule=None,
                    group="rules",
                )
                if trade is None:
                    continue
                trades.append(trade)
                busy_until = trade.exit_time
        return {
            "candidates": None,
            "skipped_overlap": skipped_overlap,
            "exits_defaulted": defaulted,
            **summarize(trades, spec.starting_equity_usdt),
        }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Window: {report['window']['started_at']} -> {report['window']['finished_at']}",
        f"Signals: {report['data']['signals']}  BUYs: {report['data']['buy_signals']}  "
        f"gate passed: {report['data']['gate_passed_buys']}  blocked: {report['data']['blocked_buys']}",
        "",
        "Portfolio level (one position at a time across the group):",
        "",
        "| group | trades | win rate | net USDT | return % | avg bps | PF | max DD % |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, group in report["groups"].items():
        lines.append(_row(name, group))
    hold = report["hold_cash"]
    lines.append(_row("hold_cash", hold))
    baseline = report["rules_baseline"]
    lines.append(_row("rules_sma", baseline))
    blocked = report["blocked_buys"]
    lines.append(_row("blocked_buys", blocked))
    lines.append("")
    lines.append(
        "rules baseline: "
        f"{baseline['exits_defaulted']} entries used assumed default exits "
        "(no live policy covered that symbol)"
    )
    lines.append("")
    lines.append("Signal level (each BUY replayed alone, gate ignored):")
    level = report["signal_level"]
    for key in ("all", "gate_passed", "gate_blocked", "gate_other"):
        part = level[key]
        lines.append(
            f"- {key}: {part['signals']} signals, {part['wins']} would have won, "
            f"avg {part['average_net_bps']} bps (stdev {part['stdev_net_bps']})"
        )
    for key in (
        "timeout_path_gate_passed",
        "timeout_path_gate_blocked",
        "timeout_path_gate_other",
    ):
        part = level[key]
        if part.get("timeouts"):
            lines.append(
                f"- {key}: {part['timeouts']} timeouts, MFE avg {part['average_mfe_bps']} bps, "
                f"MAE avg {part['average_mae_bps']} bps, "
                f"{part['timeouts_that_peaked_above_40_bps']} peaked above 40 bps"
            )
    lines.append("")
    lines.append(
        f"Blocked BUYs that would have won: {blocked['wins']} / {blocked['trades']} "
        "(each judged alone; drawdown is not meaningful for this set)"
    )
    if report.get("walk_forward"):
        lines.append("")
        lines.append(format_walk_forward(report["walk_forward"]))
    lines.append("Assumptions:")
    for item in report["assumptions"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


#: Signal states that mean the Jev gate admitted the signal.
_GATE_PASSED_STATES = frozenset(
    {
        "ELIGIBLE",
        "INTENT_CREATED",
        "RISK_PENDING",
        "RISK_REJECTED",
        "APPROVED",
        "EXECUTION_PENDING",
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "POSITION_OPEN",
        "EXIT_PENDING",
        "CLOSED",
    }
)


def _gate_bucket(state: str) -> str:
    """Which gate outcome this signal's final state records.

    ``other`` is its own bucket on purpose: a signal that errored or expired
    was never admitted by the gate, so counting it as "passed" would flatter
    the gate, and counting it as "blocked" would blame it for a provider
    failure.
    """

    if state == "REJECTED":
        return "blocked"
    if state in _GATE_PASSED_STATES:
        return "passed"
    return "other"


def _money(value: str) -> str:
    return f"{Decimal(value):.4f}"


def _row(name: str, group: dict[str, Any]) -> str:
    win_rate = group["win_rate_pct"]
    pf = group["profit_factor"]
    return (
        f"| {name} | {group['trades']} | "
        f"{'-' if win_rate is None else f'{win_rate}%'} | "
        f"{_money(group['net_pnl_usdt'])} | {group['return_pct']} | "
        f"{group['average_net_bps']} | {'-' if pf is None else pf} | "
        f"{group['max_drawdown_pct']} |"
    )


async def run_experiment(
    spec: ExperimentSpec, *, walk_forward: bool = False, folds: int = 4
) -> dict[str, Any]:
    """Build the small stack an offline replay needs, then run it."""

    from ..adapters.market.binance import BinanceMarketDataProvider
    from ..config import Settings

    settings = Settings()
    database = Database(settings.database_url)
    provider = BinanceMarketDataProvider(settings.binance_base_url)
    try:
        await database.create_all()
        repo = Repository(database)
        runner = ExperimentRunner(settings=settings, repo=repo, market=MarketService(provider))
        report = await runner.run(spec)
        if walk_forward:
            report["walk_forward"] = await runner.walk_forward(
                days=spec.days,
                fold_count=folds,
                symbols=spec.symbols,
            )
        return report
    finally:
        await provider.close()
        await database.dispose()


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--json", action="store_true", help="print the raw report")
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="add rolling train/validation/test folds and a plateau verdict",
    )
    parser.add_argument("--folds", type=int, default=4)
    args = parser.parse_args(list(argv) if argv is not None else None)
    spec = ExperimentSpec(
        symbols=tuple(symbol.strip().upper() for symbol in args.symbols.split(",")),
        days=max(1, args.days),
    )
    report = asyncio.run(run_experiment(spec, walk_forward=args.walk_forward, folds=args.folds))
    print(json.dumps(report, indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()
