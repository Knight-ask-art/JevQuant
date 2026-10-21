"""Offline A/B/C comparison over real candles and recorded Jev decisions.

Three groups, one replay engine, one cost model:

- ``rules``: a deterministic SMA20/SMA60 hourly crossover, no model involved;
- ``jev_all_buys``: every recorded Jev BUY, including the ones the gate blocked;
- ``jev_gate_passed``: only the BUYs the gate admitted.

The point is not to crown a winner on three days of data. It is to put the
same question to all three groups with the same fills, fees, exits and
one-position-at-a-time constraint, so the answer is at least comparable.

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

from ..domain.market import Candle
from ..domain.money import ZERO, canonical_str
from ..domain.policy import EffectivePolicy, ExitDefaults
from ..storage.db import Database
from ..storage.repository import Repository
from .counterfactual import Replay, simulate
from .features import aggregate, closed_candles, sma
from .market_service import MarketService

DEFAULT_EXITS = ExitDefaults(
    stop_loss_bps=Decimal("40"),
    take_profit_bps=Decimal("60"),
    max_holding_seconds=5400,
)


@dataclass(frozen=True)
class ExperimentSpec:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    days: int = 3
    end: datetime | None = None
    baseline_fast: int = 20
    baseline_slow: int = 60
    starting_equity_usdt: Decimal = Decimal("13.89")

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


class ExperimentRunner:
    def __init__(self, *, settings, repo: Repository, market: MarketService) -> None:
        self.settings = settings
        self.repo = repo
        self.market = market

    # -- data ------------------------------------------------------------
    async def _minutes(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        """Every closed 1m bar in ``[start, end]``, paged around the 1000 cap."""

        bars: list[Candle] = []
        cursor = start
        while cursor < end:
            batch = await self.market.klines(symbol, interval="1m", limit=1000, start=cursor)
            batch = [bar for bar in closed_candles(batch, end) if bar.open_time >= start]
            if not batch:
                break
            bars.extend(batch)
            if batch[-1].close_time <= cursor:
                break
            cursor = batch[-1].close_time
            if len(batch) < 1000:
                break
        return bars

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
            taker_fee_bps=Decimal("10"),
            exit_slippage_bps=self.settings.exit_slippage_bps,
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
            entry_price = snapshot.ask * (
                Decimal("1") + self.settings.slippage_bps / Decimal("10000")
            )
            return self._replay(
                symbol=row.symbol,
                bars=symbol_bars,
                entered_at=row.created_at,
                entry_price=entry_price,
                exits=policy.exit_defaults,
                notional=self._notional(policy, row.symbol),
                signal_id=row.id,
                gate="n/a",
                probability=float(row.score_json.get("entry_probability") or 0.0),
                blocked_rule=None,
                group=group.name,
            )

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
            if not gate_passed:
                # Blocked set: judged one signal at a time, no overlap filter.
                policy = await policy_for(row.policy_id)
                snapshot = await self.repo.get_market_snapshot(row.snapshot_id)
                if policy is not None and snapshot is not None and row.symbol in bars:
                    horizon_end = row.created_at + timedelta(
                        seconds=policy.exit_defaults.max_holding_seconds
                    )
                    if horizon_end <= end:
                        entry_price = snapshot.ask * (
                            Decimal("1") + self.settings.slippage_bps / Decimal("10000")
                        )
                        trade = self._replay(
                            symbol=row.symbol,
                            bars=bars[row.symbol],
                            entered_at=row.created_at,
                            entry_price=entry_price,
                            exits=policy.exit_defaults,
                            notional=self._notional(policy, row.symbol),
                            signal_id=row.id,
                            gate="blocked",
                            probability=float(row.score_json.get("entry_probability") or 0.0),
                            blocked_rule=None,
                            group="blocked_buys",
                        )
                        if trade is not None:
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
            "rules_baseline": {
                "description": f"SMA{spec.baseline_fast}/{spec.baseline_slow} hourly crossover",
                **baseline,
            },
            "blocked_buys": {
                "description": "blocked BUYs replayed independently",
                **summarize(blocked, spec.starting_equity_usdt),
            },
        }

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

        def exits_at(moment: datetime) -> ExitDefaults:
            for policy in policies:
                if policy.generated_at <= moment and policy.expires_at >= moment:
                    effective = EffectivePolicy.model_validate(policy.effective_json)
                    return effective.exit_defaults
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
                exits = exits_at(entered_at)
                entry_price = Decimal(str(hourly[index].close)) * (
                    Decimal("1") + self.settings.slippage_bps / Decimal("10000")
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
            **summarize(trades, spec.starting_equity_usdt),
        }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Window: {report['window']['started_at']} -> {report['window']['finished_at']}",
        f"Signals: {report['data']['signals']}  BUYs: {report['data']['buy_signals']}  "
        f"gate passed: {report['data']['gate_passed_buys']}  blocked: {report['data']['blocked_buys']}",
        "",
        "| group | trades | win rate | net USDT | return % | avg bps | PF | max DD % |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, group in report["groups"].items():
        lines.append(_row(name, group))
    baseline = report["rules_baseline"]
    lines.append(_row("rules_sma", baseline))
    blocked = report["blocked_buys"]
    lines.append(_row("blocked_buys", blocked))
    lines.append("")
    lines.append(f"Blocked BUYs that would have won: {blocked['wins']} / {blocked['trades']}")
    lines.append("Assumptions:")
    for item in report["assumptions"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _row(name: str, group: dict[str, Any]) -> str:
    win_rate = group["win_rate_pct"]
    pf = group["profit_factor"]
    return (
        f"| {name} | {group['trades']} | "
        f"{'-' if win_rate is None else f'{win_rate}%'} | "
        f"{group['net_pnl_usdt']} | {group['return_pct']} | "
        f"{group['average_net_bps']} | {'-' if pf is None else pf} | "
        f"{group['max_drawdown_pct']} |"
    )


async def run_experiment(spec: ExperimentSpec) -> dict[str, Any]:
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
        return await runner.run(spec)
    finally:
        await provider.close()
        await database.dispose()


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(list(argv) if argv is not None else None)
    spec = ExperimentSpec(
        symbols=tuple(symbol.strip().upper() for symbol in args.symbols.split(",")),
        days=max(1, args.days),
    )
    report = asyncio.run(run_experiment(spec))
    print(json.dumps(report, indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()
