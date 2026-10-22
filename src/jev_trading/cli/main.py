"""``jev`` command line.

Commands are deliberately small: they compose the same ``AppContainer`` the
API uses, so any behaviour observed in the CLI is identical to the service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..domain.enums import Mode
from ..logging_setup import configure_logging
from ..services.container import AppContainer


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _paper_settings(settings: Settings, database_url: str | None) -> Settings:
    """A demo must never be able to touch a real venue."""

    overrides: dict[str, Any] = {
        "trading_mode": Mode.PAPER,
        "enable_live_trading": False,
        "execution_backend": "paper",
        "synthetic_market": True,
        "demo_mode": True,
        "auth_disabled": True,
        "app_env": "dev",
    }
    if database_url:
        overrides["database_url"] = database_url
    return settings.model_copy(update=overrides)


async def _doctor(settings: Settings) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    check("database_dir", True, settings.database_url)
    check("auth_config", not (settings.app_env == "prod" and settings.auth_disabled),
          "auth disabled" if settings.auth_disabled else "auth enabled")
    check(
        "live_env_guard",
        not (settings.enable_live_trading and settings.trading_mode.value != "live"),
        f"trading_mode={settings.trading_mode.value}",
    )
    check(
        "live_key_policy",
        not settings.enable_live_trading or bool(settings.mfa_totp_secret),
        "MFA required for live arming",
    )

    container = AppContainer(settings)
    try:
        await container.db.create_all()
        check("database", True, "schema reachable")
        await container.broker.start()
        check("redis", not settings.redis_url or container.broker.redis_healthy,
              "not configured" if not settings.redis_url else "configured")
        await container.modes.initialize()
        mode = await container.modes.current_mode()
        check("mode_restored_paper", mode.value in {"paper", "shadow", "testnet"},
              mode.value)
        market = await container.market.health()
        check("market_feed", bool(market.get("healthy")), str(market.get("source", "")))
        audit = await container.repo.verify_audit_chain()
        check("audit_chain", bool(audit.get("valid")), f"{audit.get('count', 0)} events")
    except Exception as exc:  # noqa: BLE001
        check("startup", False, type(exc).__name__)
    finally:
        await container.close()

    failed = [item for item in checks if not item["passed"]]
    _print(
        {
            "ok": not failed,
            "generated_at": datetime.now(UTC).isoformat(),
            "config": settings.redacted_summary(),
            "checks": checks,
        }
    )
    return 0 if not failed else 1


async def _run_cycles(settings: Settings, count: int, refresh_policy: bool) -> int:
    container = await AppContainer(settings).start()
    try:
        results = []
        for _ in range(count):
            results.append(await container.run_cycle(refresh_policy=refresh_policy))
        status = await container.status()
        audit = await container.audit.verify()
        last = results[-1].model_dump(mode="json") if results else None
        _print(
            {
                "cycles": results[-1].cycle_id if results else None,
                "mode": status["mode"]["mode"],
                "data_source": status["data_source"],
                "synthetic_market": status["synthetic_market"],
                "last_cycle": last,
                "account": status["account"],
                "positions": status["positions"],
                "audit": audit,
                "note": (
                    "Synthetic market and simulated models: this is a plumbing "
                    "check, not a performance result."
                ),
            }
        )
        return 0
    finally:
        await container.close()


async def _status(settings: Settings) -> int:
    configure_logging()
    container = await AppContainer(settings).start()
    try:
        _print(await container.status())
        return 0
    finally:
        await container.close()


def _serve(settings: Settings, host: str, port: int, reload: bool) -> int:
    configure_logging()
    import uvicorn

    if reload:
        uvicorn.run(
            "jev_trading.api.app:app", host=host, port=port, reload=True
        )
        return 0
    from ..api.app import create_app

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


def _backtest(settings: Settings, args: argparse.Namespace) -> int:
    from ..services.backtest import BacktestSpec, run_backtest

    payload = {
        "symbols": [item.strip().upper() for item in args.symbols.split(",") if item.strip()],
        "minutes": args.minutes,
        "candles": args.candles,
        "seed": args.seed,
        "starting_equity_usdt": "10000",
    }
    unknown = sorted(set(payload["symbols"]) - set(settings.allowed_symbols))
    if unknown:
        _print({"error": f"symbols outside the allowed universe: {unknown}"})
        return 2
    report = run_backtest(BacktestSpec.from_payload(payload))
    _print(
        {
            "summary": report["summary"],
            "per_symbol": {
                symbol: {
                    key: value
                    for key, value in result.items()
                    if key != "equity_curve"
                }
                for symbol, result in report["per_symbol"].items()
            },
            "assumptions": report["assumptions"],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev",
        description="Jev + LLM dual-model trading system (paper-first).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override JEV_DATABASE_URL (sqlite+aiosqlite:///./data/jev.db).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Offline prerequisite and configuration check.")

    migrate_parser = sub.add_parser(
        "migrate", help="Apply pending database schema migrations."
    )
    migrate_parser.add_argument("--dry-run", action="store_true", help="List only.")

    demo = sub.add_parser(
        "demo", help="Run paper cycles against the synthetic market."
    )
    demo.add_argument("--cycles", type=int, default=1)
    demo.add_argument("--refresh-policy", action="store_true")

    cycle = sub.add_parser("cycle", help="Run exactly one trading cycle.")
    cycle.add_argument("--refresh-policy", action="store_true")
    cycle.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required when the configured mode is live (still subject to arming).",
    )

    status = sub.add_parser("status", help="Print the full system status as JSON.")
    status.add_argument("--json", action="store_true", help="Accepted for symmetry.")

    serve = sub.add_parser("serve", help="Start the FastAPI control plane.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    backtest = sub.add_parser("backtest", help="Run a deterministic synthetic replay.")
    backtest.add_argument("--symbols", default="BTCUSDT")
    backtest.add_argument("--minutes", type=int, default=5)
    backtest.add_argument("--candles", type=int, default=1500)
    backtest.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.db:
        settings = settings.model_copy(update={"database_url": args.db})
    if settings.database_url.startswith("sqlite") and ":///" in settings.database_url:
        raw = settings.database_url.split(":///", 1)[1]
        path = Path(raw)
        if path.parent and str(path.parent) not in {"", "."}:
            path.parent.mkdir(parents=True, exist_ok=True)

    if args.command == "migrate":
        from ..migrations import main as migrate_main

        extra = ["--db", settings.database_url] + (["--dry-run"] if args.dry_run else [])
        return migrate_main(extra)
    if args.command == "doctor":
        return asyncio.run(_doctor(settings))
    if args.command == "status":
        return asyncio.run(_status(settings))
    if args.command == "demo":
        return asyncio.run(
            _run_cycles(
                _paper_settings(settings, args.db), args.cycles, args.refresh_policy
            )
        )
    if args.command == "cycle":
        if settings.trading_mode is Mode.LIVE and not args.confirm_live:
            _print(
                {
                    "error": "Refusing to run a live cycle without --confirm-live.",
                    "hint": "The live gate also requires an armed session.",
                }
            )
            return 2
        return asyncio.run(_run_cycles(settings, 1, args.refresh_policy))
    if args.command == "serve":
        return _serve(settings, args.host, args.port, args.reload)
    if args.command == "backtest":
        return _backtest(settings, args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
