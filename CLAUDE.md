# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

JevQuant is a lightweight automated trading research system pairing **TypeSafe Jev** with **Binance Spot BTC/USDT**. It computes technical indicators on closed 1h candles, queries Jev for structured signals (BUY / SELL / HOLD), evaluates them through an independent risk manager, and executes via an identical interface across Paper, Testnet, and micro-budget Live trading (50 CNY capital cap).

Key project specifications:
- Machine specification: `JevQuant.spec` (highest priority source of truth).
- Phased implementation guide: `JevQuant-开发计划.md`.

## Core Invariants & Safety Constraints

1. **AI / Execution Separation**: Jev outputs only high-level directional advice (`BUY`, `SELL`, `HOLD`). Jev cannot create orders, dictate amounts, modify risk policies, or alter exchange keys.
2. **Exit Feasibility Pre-check**: Before placing a buy order, the risk manager must verify that in an adverse price move (e.g., -3% drop minus fees/slippage), the position can still be legally sold under Binance `MIN_NOTIONAL` / `NOTIONAL` and `LOT_SIZE` rules. If not, the buy must be rejected (`REJECT_EXIT_MIN_NOTIONAL`).
3. **Strict Money & Quantity Precision**: Always use `decimal.Decimal` for financial quantities, prices, notional calculations, and step sizing. Never use binary floats for order sizing or limits.
4. **Fail-Closed on Market Rules**: Market filters must be dynamically queried via `exchangeInfo` and never hardcoded to static values. If rules are missing or the symbol is not in `TRADING` status, halt and refuse to trade.
5. **No Secret Leaks**: Never log, expose via UI, or persist API secrets or authorization headers.
6. **Live Trading Gate**: Live mode requires `TRADING_MODE=live` + `ENABLE_LIVE_TRADING=true` + explicit CLI `--confirm-live` + preflight inspection pass. Default is always `paper`.
7. **Frontend Design Style**: The dashboard (Phase 3+) follows a dark-tech / industrial terminal aesthetic inspired by "Jev Market Reflex" (black canvas, green/red/amber status indicators, real-time metrics, circular reflex latency radar, heartbeat timeline, and structured action log).

## Development Commands

```powershell
# Virtual environment (Python 3.13)
.\.venv\Scripts\activate

# Install dependencies (editable mode)
pip install -e ".[dev]"

# Run test suite
pytest

# Run a single test file
pytest tests/test_exchange_rules.py -v

# Run a specific test function
pytest tests/test_exchange_rules.py -k "test_filter_parsing" -v

# Run public market & filter probe (requires no API keys)
python -m jevquant.exchange
```

## Architecture & Code Structure

The project follows a standard Python `src`-layout under `src/jevquant/`:

- `config.py`: Environment configuration, mode validation, strict live preflight guards.
- `storage.py`: SQLite persistence (candles, market_rules_snapshots, signals, risk_checks, orders, fills, balances) using parameterized queries and Decimal strings.
- `exchange.py`: CCXT Binance Spot adapter, rule parsing (MIN_NOTIONAL, LOT_SIZE, PRICE_FILTER), and candle fetching (closed candles only).
- `indicators.py`: Pandas-based technical indicators (SMA20, SMA60, RSI14, ATR14) without lookahead bias.
- `jev_engine.py`: Official `typesafe-sdk` wrapper; defaults to `HOLD` on timeout or API error.
- `strategy.py`: Jev strategy and baseline comparison strategy (e.g. SMA crossover).
- `risk.py`: 50 CNY budget allocation, step-size rounding, exit feasibility checker, and daily loss circuit breakers.
- `orders.py`: Order state machine (`FLAT` -> `ENTRY_SUBMITTING` -> `LONG` -> `EXIT_SUBMITTING`), idempotency via `client_order_id`, and restart reconciliation.
- `runner.py`: 1h closed-candle trigger, periodic workflow coordinator, and graceful shutdown.
- `dashboard/`: Real-time monitoring UI matching the dark terminal "Jev Market Reflex" visual design.
