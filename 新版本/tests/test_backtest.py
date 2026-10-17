"""Backtests are deterministic and honest about being synthetic."""

from jev_trading.services.backtest import BacktestSpec, run_backtest


def test_same_seed_produces_identical_results():
    spec = BacktestSpec(symbols=("BTCUSDT",), candles=400, seed=99)
    first = run_backtest(spec)
    second = run_backtest(spec)
    assert first["summary"] == second["summary"]
    assert first["trades"] == second["trades"]


def test_different_seed_changes_results():
    first = run_backtest(BacktestSpec(symbols=("BTCUSDT",), candles=400, seed=1))
    second = run_backtest(BacktestSpec(symbols=("BTCUSDT",), candles=400, seed=2))
    assert first["summary"] != second["summary"]


def test_report_is_explicitly_synthetic():
    report = run_backtest(BacktestSpec(symbols=("BTCUSDT", "SOLUSDT"), candles=300))
    assert report["is_synthetic"] is True
    assert report["engine"] == "deterministic_replay_v1"
    assert report["assumptions"]
    assert "seeded random walk" in report["assumptions"][0]
    assert set(report["per_symbol"]) == {"BTCUSDT", "SOLUSDT"}
    assert DecimalLike(report["summary"]["net_pnl_usdt"])


def DecimalLike(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
