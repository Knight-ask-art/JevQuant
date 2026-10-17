"""Application composition root."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..adapters.binance_account import BinanceAccountClient
from ..adapters.execution.binance_testnet import BinanceTestnetExecutionAdapter
from ..adapters.execution.hummingbot import HummingbotExecutionAdapter
from ..adapters.execution.paper import PaperExecutionAdapter
from ..adapters.execution.shadow import ShadowExecutionAdapter
from ..adapters.market.binance import BinanceMarketDataProvider
from ..adapters.market.synthetic import SyntheticMarketDataProvider
from ..adapters.models.jev import JevAdapter
from ..adapters.models.llm import ConsoleLLMAdapter
from ..adapters.models.simulated import SimulatedJevEvaluator, SimulatedPolicyGenerator
from ..config import Settings, get_settings
from ..migrations import migrate
from ..domain.enums import ErrorCode, Mode
from ..domain.errors import AppError
from ..domain.money import canonical_str
from ..domain.policy import PolicyCompiler
from ..domain.risk import RiskEngine
from ..storage.db import Database
from ..storage.repository import Repository
from .audit import AuditService
from .events import EventBroker
from .market_service import MarketService
from .modes import ModeManager
from .orchestrator import TradingOrchestrator
from .policy_service import PolicyService

logger = logging.getLogger("jev_trading.container")


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.repo = Repository(self.db)
        self.audit = AuditService(self.repo)
        self.broker = EventBroker(settings.redis_url)
        self.market_provider = (
            SyntheticMarketDataProvider(
                seed=settings.paper_seed,
                symbols=settings.allowed_symbols,
                advance_each_call=settings.demo_mode,
            )
            if settings.synthetic_market
            else BinanceMarketDataProvider(settings.binance_base_url)
        )
        self.market = MarketService(
            self.market_provider,
            entry_filter_ttl_seconds=settings.entry_filter_ttl_seconds,
        )
        self.compiler = PolicyCompiler(settings.server_limits)
        self.policy_service = PolicyService(
            self.repo, self.compiler, settings.server_limits, self.audit, self.broker
        )
        self.policy_generator = self._build_policy_generator()
        self.jev = self._build_jev()
        self.risk_engine = RiskEngine()

        self.paper = PaperExecutionAdapter(self.repo, seed=settings.paper_seed)
        self.shadow = ShadowExecutionAdapter(self.repo)
        self.testnet = BinanceTestnetExecutionAdapter(
            self.repo,
            base_url=settings.binance_testnet_base_url,
            api_key=(
                settings.binance_testnet_api_key.get_secret_value()
                if settings.binance_testnet_api_key
                else None
            ),
            api_secret=(
                settings.binance_testnet_api_secret.get_secret_value()
                if settings.binance_testnet_api_secret
                else None
            ),
        )
        self.hummingbot = HummingbotExecutionAdapter(
            self.repo,
            base_url=settings.hummingbot_base_url,
            api_key=(
                settings.hummingbot_api_key.get_secret_value()
                if settings.hummingbot_api_key
                else None
            ),
        )
        self.binance_account = BinanceAccountClient(
            base_url=settings.binance_base_url,
            api_key=(
                settings.binance_api_key.get_secret_value()
                if settings.binance_api_key
                else None
            ),
            api_secret=(
                settings.binance_api_secret.get_secret_value()
                if settings.binance_api_secret
                else None
            ),
        )
        self.execution_for_mode = {
            Mode.PAPER: self.paper,
            Mode.SHADOW: self.shadow,
            Mode.TESTNET: self.testnet,
            Mode.LIVE: self.hummingbot,
        }
        self.modes = ModeManager(
            repo=self.repo,
            settings=settings,
            audit=self.audit,
            broker=self.broker,
            market=self.market,
            hummingbot=self.hummingbot,
            binance_account=self.binance_account,
        )
        self.account_id = ""
        self.orchestrator: TradingOrchestrator | None = None
        self._cycle_task: asyncio.Task[None] | None = None
        # One cycle at a time: the scheduler and manual HTTP cycles share the
        # ledger and must not interleave position reads with writes.
        self._cycle_lock = asyncio.Lock()

    def _build_policy_generator(self):
        settings = self.settings
        if (
            settings.opencode_console_enabled
            and settings.opencode_console_base_url
            and settings.opencode_console_api_key
            and settings.opencode_console_model
        ):
            return ConsoleLLMAdapter(
                base_url=settings.opencode_console_base_url,
                api_key=settings.opencode_console_api_key.get_secret_value(),
                model=settings.opencode_console_model,
                protocol=settings.opencode_console_protocol,
                purpose="trade_runtime",
            )
        return SimulatedPolicyGenerator(settings.server_limits)

    def _build_jev(self):
        settings = self.settings
        if settings.jev_enabled and settings.jev_base_url and settings.jev_api_key:
            return JevAdapter(
                base_url=settings.jev_base_url,
                api_key=settings.jev_api_key.get_secret_value(),
                model=settings.jev_model,
                path=settings.jev_path,
            )
        return SimulatedJevEvaluator()

    async def start(self) -> AppContainer:
        await self.db.create_all()
        # Upgrade databases created by earlier releases (create_all never
        # alters existing tables). A schema that cannot be upgraded must stop
        # the service rather than run on a half-known schema.
        outcomes = await migrate(self.db)
        mismatched = [o.version for o in outcomes if o.status == "checksum_mismatch"]
        if mismatched:
            raise RuntimeError(
                f"Applied migrations changed since they ran: {', '.join(mismatched)}"
            )
        await self.broker.start()
        await self.modes.initialize()
        account = await self.repo.get_or_create_account(
            "main", self.settings.trading_mode, self.settings.paper_starting_equity
        )
        self.account_id = account.id
        self.modes.account_id = account.id
        self.orchestrator = TradingOrchestrator(
            settings=self.settings,
            repo=self.repo,
            policy_service=self.policy_service,
            market=self.market,
            jev=self.jev,
            policy_generator=self.policy_generator,
            risk_engine=self.risk_engine,
            execution_for_mode=self.execution_for_mode,
            modes=self.modes,
            audit=self.audit,
            broker=self.broker,
            account_id=self.account_id,
        )
        await self.audit.record(
            "system.started",
            "system",
            None,
            {
                "mode": (await self.modes.current_mode()).value,
                "data_source": self.market.source,
                "execution_backend": self.settings.execution_backend,
                "synthetic": self.settings.synthetic_market,
                "auto_cycle_seconds": self.settings.auto_cycle_seconds,
            },
        )
        if self.settings.auto_cycle_seconds > 0:
            self._cycle_task = asyncio.create_task(self._auto_cycle_loop())
        return self

    async def _auto_cycle_loop(self) -> None:
        """Run cycles on a fixed wall-clock cadence.

        A failed cycle is audited and the loop continues: one provider outage
        must not silently stop the scheduler. New entries are blocked by the
        orchestrator's fail-closed checks while a dependency is unhealthy.
        """

        interval = max(1, self.settings.auto_cycle_seconds)
        while True:
            try:
                await asyncio.sleep(interval)
                # Never queue cycles up behind each other: a slow cycle lets
                # the next interval fire, and the scheduler should skip it.
                result = await self.run_cycle(wait=False)
                logger.info(
                    "auto cycle %s mode=%s signals=%s executed=%s",
                    result.cycle_id,
                    result.mode,
                    result.signals_created,
                    result.executed,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("auto cycle failed")
                try:
                    await self.audit.record(
                        "cycle.scheduler_error",
                        "cycle",
                        None,
                        {"error": type(exc).__name__},
                    )
                except Exception:  # noqa: BLE001
                    pass

    async def dependencies(self) -> dict[str, Any]:
        mode = await self.modes.current_mode()
        adapter = self.execution_for_mode.get(mode)
        market_health = await self.market.health()
        execution_health = (
            await adapter.health() if adapter is not None else {"healthy": False}
        )
        model_health = await self.policy_generator.health()
        jev_health = await self.jev.health()
        try:
            await self.repo.get_state("mode")
            db_healthy = True
        except Exception:  # noqa: BLE001
            db_healthy = False
        policy = await self.policy_service.get_active(mode.value)
        return {
            "database": {"healthy": db_healthy, "scheme": self.settings.database_url.split(":", 1)[0]},
            "redis": self.broker.health(),
            "market": market_health,
            "execution": execution_health,
            "policy_model": model_health,
            "jev": jev_health,
            "policy_fresh": policy is not None,
        }

    async def status(self) -> dict[str, Any]:
        mode_status = await self.modes.status()
        dependencies = await self.dependencies()
        marks = await self.repo.latest_market_marks()
        account = await self.repo.account_snapshot(self.account_id, marks)
        positions = await self.repo.get_open_positions(self.account_id)
        policy_row = await self.repo.get_active_policy()
        policy_payload = None
        if policy_row is not None:
            policy_payload = {
                "policy_id": policy_row.policy_id,
                "status": policy_row.status,
                "source": policy_row.source,
                "simulated": policy_row.is_simulated,
                "policy_hash": policy_row.policy_hash,
                "generated_at": policy_row.generated_at.isoformat(),
                "expires_at": policy_row.expires_at.isoformat(),
                "raw": policy_row.raw_json,
                "effective": policy_row.effective_json,
                "clamps": policy_row.clamps_json,
            }
        position_payload = []
        for position in positions:
            mark = marks.get(position.symbol, position.avg_entry_price)
            position_payload.append(
                {
                    "id": position.id,
                    "symbol": position.symbol,
                    "qty": canonical_str(position.qty),
                    "avg_entry_price": canonical_str(position.avg_entry_price),
                    "mark_price": canonical_str(mark),
                    "unrealized_pnl_usdt": canonical_str(
                        position.qty * (mark - position.avg_entry_price)
                    ),
                    "stop_loss_bps": canonical_str(position.stop_loss_bps),
                    "take_profit_bps": canonical_str(position.take_profit_bps),
                    "opened_at": position.opened_at.isoformat(),
                    "max_holding_seconds": position.max_holding_seconds,
                }
            )
        return {
            "service": self.settings.service_name,
            "app_env": self.settings.app_env,
            "server_time": datetime.now(UTC).isoformat(),
            "mode": mode_status,
            "dependencies": dependencies,
            "data_source": self.market.source,
            "synthetic_market": self.settings.synthetic_market,
            "simulated_models": (
                self.settings.synthetic_market
                or not self.settings.jev_enabled
                or not self.settings.opencode_console_enabled
            ),
            "account": {
                "id": self.account_id,
                "equity_usdt": canonical_str(account.equity_usdt),
                "available_quote_usdt": canonical_str(account.available_quote_usdt),
                "gross_exposure_usdt": canonical_str(account.gross_exposure_usdt),
                "open_positions": account.open_positions,
                "daily_realized_pnl_usdt": canonical_str(account.daily_realized_pnl_usdt),
                "daily_realized_pnl_pct": canonical_str(account.daily_realized_pnl_pct),
                "drawdown_pct": canonical_str(account.drawdown_pct),
            },
            "positions": position_payload,
            "policy": policy_payload,
            "server_limits": {
                "max_position_notional_usdt": str(
                    self.settings.max_position_notional_usdt
                ),
                "max_gross_exposure_pct": str(self.settings.max_gross_exposure_pct),
                "max_daily_loss_pct": str(self.settings.max_daily_loss_pct),
                "max_positions": self.settings.max_positions,
                "max_drawdown_pct": str(self.settings.max_drawdown_pct),
                "allowed_symbols": list(self.settings.allowed_symbols),
            },
            "execution_backend": self.settings.execution_backend,
        }

    async def run_cycle(self, *, refresh_policy: bool = False, wait: bool = True):
        orchestrator = self.orchestrator
        if orchestrator is None:
            raise RuntimeError("container has not been started")
        if not self._cycle_lock.locked():
            async with self._cycle_lock:
                return await orchestrator.run_cycle(refresh_policy=refresh_policy)
        if not wait:
            raise AppError(
                ErrorCode.CONFLICT,
                "A cycle is already running; retry when it finishes.",
            )
        async with self._cycle_lock:
            return await orchestrator.run_cycle(refresh_policy=refresh_policy)

    async def close(self) -> None:
        if self._cycle_task is not None:
            self._cycle_task.cancel()
            try:
                await self._cycle_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._cycle_task = None
        await self.market.close()
        await self.paper.close()
        await self.shadow.close()
        await self.testnet.close()
        await self.hummingbot.close()
        await self.binance_account.close()
        if hasattr(self.policy_generator, "close"):
            await self.policy_generator.close()  # type: ignore[attr-defined]
        if hasattr(self.jev, "close"):
            await self.jev.close()  # type: ignore[attr-defined]
        await self.broker.close()
        await self.db.dispose()
