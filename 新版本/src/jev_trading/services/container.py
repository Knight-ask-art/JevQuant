"""Application composition root."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, AsyncIterator

from pydantic import SecretStr

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
from ..domain.market import Candle, MarketSnapshot
from ..domain.policy import PolicyCompiler
from ..domain.risk import RiskEngine
from ..storage.db import Database
from ..storage.repository import Repository
from .audit import AuditService
from .balance_sync import BalanceSyncScheduler, BalanceSyncService
from .events import EventBroker
from .features import build_features
from .market_service import MarketService
from .modes import ModeManager
from .orchestrator import TradingOrchestrator
from .policy_service import PolicyService

logger = logging.getLogger("jev_trading.container")


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.default_universe = list(settings.allowed_symbols)
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
        # Position/ledger writers: a trading cycle and a venue reconciliation
        # both read open positions and apply settlements. They must not
        # interleave, whether the reconcile is operator-triggered or fired by a
        # balance drift.
        self.ledger_lock = asyncio.Lock()
        self.balance_sync: BalanceSyncService | None = None
        self._balance_task: asyncio.Task[None] | None = None
        # Runtime automation. The interval is mutable so the UI can switch the
        # scheduler on or off without restarting the process.
        self._auto_interval = settings.auto_cycle_seconds
        self._automation_enabled = settings.auto_cycle_seconds > 0
        self._next_cycle_at: datetime | None = None
        self._automation_lock = asyncio.Lock()
        # Serializes changes to the symbol universe: the settings, compiler
        # and simulator must move together.
        self._settings_lock = asyncio.Lock()

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
        await self._load_symbol_universe()
        # Credentials entered through the UI outlive a container restart, so
        # load them before the orchestrator is wired to the adapters.
        await self._load_runtime_settings()
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
                "balance_sync_seconds": self.settings.balance_sync_seconds,
                "balance_auto_reconcile": self.settings.balance_auto_reconcile,
                "balance_reconcile_cooldown_seconds": (
                    self.settings.balance_reconcile_cooldown_seconds
                ),
            },
        )
        self.balance_sync = BalanceSyncService(
            repo=self.repo,
            audit=self.audit,
            broker=self.broker,
            adapters=self.execution_for_mode,
            account_id=self.account_id,
            base_assets={symbol: symbol.removesuffix("USDT") for symbol in self.settings.allowed_symbols},
            auto_reconcile=self.settings.balance_auto_reconcile,
            reconcile_cooldown_seconds=self.settings.balance_reconcile_cooldown_seconds,
            reconcile_lock=self.ledger_lock,
        )
        if self.settings.balance_sync_seconds > 0:
            scheduler = BalanceSyncScheduler(
                self.balance_sync, interval_seconds=self.settings.balance_sync_seconds
            )
            self._balance_task = asyncio.create_task(scheduler.loop())
        return self

    # ------------------------------------------------------------------
    # symbol universe
    # ------------------------------------------------------------------
    def symbol_universe(self) -> list[str]:
        return list(self.settings.allowed_symbols)

    @asynccontextmanager
    async def settings_lock(self) -> AsyncIterator[None]:
        async with self._settings_lock:
            yield

    async def apply_symbol_universe(self, symbols: list[str], *, actor: str) -> None:
        """Make ``symbols`` the hard envelope for every downstream component.

        The orchestrator reads ``self.settings`` each cycle, so mutating the
        settings object is enough; the compiler's snapshot and the synthetic
        simulator are rebuilt explicitly. Callers hold ``settings_lock()``.
        """

        normalized = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        if not normalized:
            raise ValueError("the symbol universe cannot be empty")
        self.settings.allowed_symbols = normalized
        self.compiler = PolicyCompiler(self.settings.server_limits)
        self.policy_service.compiler = self.compiler
        self.policy_service.server_limits = self.settings.server_limits
        if isinstance(self.market_provider, SyntheticMarketDataProvider):
            self.market_provider.symbols = list(normalized)
        await self.repo.set_state("symbol_universe", normalized)
        await self.audit.record(
            "settings.universe_updated",
            "settings",
            None,
            {"symbols": normalized, "actor": actor},
        )

    async def _load_symbol_universe(self) -> None:
        stored = await self.repo.get_state("symbol_universe")
        if isinstance(stored, list) and stored:
            symbols = [str(item).upper() for item in stored]
        else:
            env = self.settings.allowed_symbols
            if self.settings.synthetic_market and isinstance(
                self.market_provider, SyntheticMarketDataProvider
            ):
                # The synthetic feed can only serve symbols it knows.
                symbols = [item for item in env if item in self.market_provider.symbols]
            else:
                symbols = env
        if symbols and symbols != self.settings.allowed_symbols:
            await self.apply_symbol_universe(symbols, actor="system:startup")

    # ------------------------------------------------------------------
    # runtime configuration (persisted in system_state, never echoed back)
    # ------------------------------------------------------------------
    async def _load_runtime_settings(self) -> None:
        stored = await self.repo.get_state("runtime_credentials")
        if isinstance(stored, dict):
            self._apply_credentials(stored)
        automation = await self.repo.get_state("automation")
        if isinstance(automation, dict) and automation.get("enabled"):
            interval = int(automation.get("interval_seconds") or 300)
            self._start_auto_loop(interval)
        elif self.settings.auto_cycle_seconds > 0:
            self._start_auto_loop(self.settings.auto_cycle_seconds)

    def model_settings(self) -> dict[str, Any]:
        """Non-secret view of the model connections."""

        settings = self.settings
        generator_provider = getattr(self.policy_generator, "provider", "simulated")
        return {
            "llm": {
                "provider": generator_provider,
                "configured": bool(
                    settings.opencode_console_enabled
                    and settings.opencode_console_base_url
                    and settings.opencode_console_api_key
                    and settings.opencode_console_model
                ),
                "api_key_configured": bool(settings.opencode_console_api_key),
                "base_url": settings.opencode_console_base_url,
                "model": settings.opencode_console_model,
                "protocol": settings.opencode_console_protocol,
                "simulated": generator_provider == "simulated",
            },
            "jev": {
                "provider": (
                    "simulated" if isinstance(self.jev, SimulatedJevEvaluator) else "jev"
                ),
                "configured": bool(
                    settings.jev_enabled and settings.jev_base_url and settings.jev_api_key
                ),
                "api_key_configured": bool(settings.jev_api_key),
                "base_url": settings.jev_base_url,
                "model": getattr(self.jev, "model_name", "jev-simulated-1.13"),
                "path": settings.jev_path,
                "simulated": isinstance(self.jev, SimulatedJevEvaluator),
            },
        }

    def _merge_provider(
        self,
        current: dict[str, Any],
        payload: dict[str, Any],
        *,
        existing_key: SecretStr | None,
    ) -> dict[str, Any]:
        """Merge a write-only credential payload with what is already stored.

        An omitted or empty ``api_key`` keeps the stored secret: the UI never
        receives it back, so it cannot echo it on the next save.
        """

        base_url = payload["base_url"] if "base_url" in payload else current.get("base_url")
        model = payload["model"] if "model" in payload else current.get("model")
        if payload.get("clear_api_key"):
            api_key = None
        else:
            supplied = payload.get("api_key")
            api_key = (
                supplied
                if supplied
                else (existing_key.get_secret_value() if existing_key else None)
            )
        enabled = payload.get("enabled")
        if enabled is None:
            enabled = bool(base_url and model and api_key)
        merged = {
            "enabled": bool(enabled),
            "base_url": base_url or None,
            "api_key": api_key or None,
            "model": model or None,
        }
        if "protocol" in payload:
            merged["protocol"] = payload["protocol"]
        if "path" in payload:
            merged["path"] = payload["path"]
        return merged

    def _apply_credentials(self, stored: dict[str, Any]) -> None:
        """Apply persisted credentials and rebuild both model adapters."""
        settings = self.settings
        llm = stored.get("llm")
        if isinstance(llm, dict):
            settings.opencode_console_enabled = bool(llm.get("enabled"))
            settings.opencode_console_base_url = llm.get("base_url") or None
            key = llm.get("api_key")
            settings.opencode_console_api_key = SecretStr(key) if key else None
            settings.opencode_console_model = llm.get("model") or None
            settings.opencode_console_protocol = llm.get("protocol") or "openai"
        jev = stored.get("jev")
        if isinstance(jev, dict):
            settings.jev_enabled = bool(jev.get("enabled"))
            settings.jev_base_url = jev.get("base_url") or None
            key = jev.get("api_key")
            settings.jev_api_key = SecretStr(key) if key else None
            settings.jev_model = jev.get("model") or settings.jev_model
            settings.jev_path = jev.get("path") or settings.jev_path

        previous_generator = self.policy_generator
        previous_jev = self.jev
        self.policy_generator = self._build_policy_generator()
        self.jev = self._build_jev()
        if self.orchestrator is not None:
            self.orchestrator.policy_generator = self.policy_generator
            self.orchestrator.jev = self.jev
        retired = []
        for adapter in (previous_generator, previous_jev):
            if adapter is not self.policy_generator and adapter is not self.jev:
                retired.append(adapter)
        for adapter in retired:
            close = getattr(adapter, "close", None)
            if close is not None:
                self._schedule_close(close)

    @staticmethod
    def _schedule_close(close) -> None:
        try:
            asyncio.get_running_loop().create_task(close())
        except RuntimeError:  # no running loop (unit tests)
            pass

    async def update_model_settings(
        self, *, llm: dict[str, Any] | None, jev: dict[str, Any] | None, actor: str
    ) -> dict[str, Any]:
        stored = await self.repo.get_state("runtime_credentials", {})
        if not isinstance(stored, dict):
            stored = {}
        current = self.model_settings()
        if llm is not None:
            existing_llm = stored.get("llm")
            current_key = (
                SecretStr(existing_llm["api_key"])
                if isinstance(existing_llm, dict) and existing_llm.get("api_key")
                else self.settings.opencode_console_api_key
            )
            merged = self._merge_provider(current["llm"], llm, existing_key=current_key)
            merged.setdefault("protocol", current["llm"].get("protocol") or "openai")
            stored["llm"] = merged
        if jev is not None:
            existing_jev = stored.get("jev")
            current_key = (
                SecretStr(existing_jev["api_key"])
                if isinstance(existing_jev, dict) and existing_jev.get("api_key")
                else self.settings.jev_api_key
            )
            merged = self._merge_provider(current["jev"], jev, existing_key=current_key)
            merged.setdefault("path", current["jev"].get("path") or "/v1/inference")
            stored["jev"] = merged
        # Swap adapters while no cycle can be reading them.
        async with self._cycle_lock:
            async with self.ledger_lock:
                self._apply_credentials(stored)
        await self.repo.set_state("runtime_credentials", stored)
        await self.audit.record(
            "settings.models_updated",
            "settings",
            None,
            {
                "llm_configured": self.model_settings()["llm"]["configured"],
                "jev_configured": self.model_settings()["jev"]["configured"],
                "actor": actor,
            },
        )
        return {
            "models": self.model_settings(),
            "health": {
                "llm": await self._bounded_health(self.policy_generator),
                "jev": await self._bounded_health(self.jev),
            },
        }

    @staticmethod
    async def _bounded_health(adapter, timeout: float = 5.0) -> dict[str, Any]:
        """Probe a freshly built adapter without letting the UI hang."""

        try:
            return await asyncio.wait_for(adapter.health(), timeout=timeout)
        except TimeoutError:
            return {"healthy": False, "error": "timeout"}
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "error": type(exc).__name__}

    # ------------------------------------------------------------------
    # runtime automation
    # ------------------------------------------------------------------
    def automation_status(self) -> dict[str, Any]:
        return {
            "enabled": self._automation_enabled,
            "interval_seconds": self._auto_interval,
            "next_run_at": (
                self._next_cycle_at.isoformat()
                if self._automation_enabled and self._next_cycle_at is not None
                else None
            ),
            "running": self._cycle_task is not None and not self._cycle_task.done(),
        }

    async def set_automation(
        self, *, enabled: bool, interval_seconds: int, actor: str
    ) -> dict[str, Any]:
        async with self._automation_lock:
            if enabled:
                self._start_auto_loop(interval_seconds)
            else:
                await self._stop_auto_loop()
            await self.repo.set_state(
                "automation",
                {
                    "enabled": self._automation_enabled,
                    "interval_seconds": self._auto_interval,
                },
            )
        await self.audit.record(
            "settings.automation_updated",
            "settings",
            None,
            {
                "enabled": self._automation_enabled,
                "interval_seconds": self._auto_interval,
                "actor": actor,
            },
        )
        return self.automation_status()

    def _start_auto_loop(self, interval_seconds: int) -> None:
        interval = max(10, int(interval_seconds))
        if self._cycle_task is not None and not self._cycle_task.done():
            self._cycle_task.cancel()
        self._auto_interval = interval
        self._automation_enabled = True
        self._next_cycle_at = datetime.now(UTC) + timedelta(seconds=interval)
        self._cycle_task = asyncio.create_task(self._auto_cycle_loop())

    async def _stop_auto_loop(self) -> None:
        self._automation_enabled = False
        self._next_cycle_at = None
        task = self._cycle_task
        self._cycle_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _auto_cycle_loop(self) -> None:
        """Run cycles on a cadence the operator can change at runtime.

        A failed cycle is audited and the loop continues: one provider outage
        must not silently stop the scheduler. New entries are blocked by the
        orchestrator's fail-closed checks while a dependency is unhealthy.
        """
        while True:
            try:
                interval = max(10, self._auto_interval)
                await asyncio.sleep(interval)
                if not self._automation_enabled:
                    return
                self._next_cycle_at = None
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
            except AppError as exc:
                # A manual cycle already holds the lock; skip this tick rather
                # than recording a scheduler failure.
                if exc.code is not ErrorCode.CONFLICT:
                    logger.exception("auto cycle failed")
            except Exception:  # noqa: BLE001
                logger.exception("auto cycle failed")
                try:
                    await self.audit.record(
                        "cycle.scheduler_error",
                        "cycle",
                        None,
                        {"error": "unexpected"},
                    )
                except Exception:  # noqa: BLE001
                    pass
            finally:
                if self._automation_enabled:
                    self._next_cycle_at = datetime.now(UTC) + timedelta(
                        seconds=max(10, self._auto_interval)
                    )

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
                    "mode": position.mode,
                    "is_simulated": position.is_simulated,
                    "exit_requested_at": (
                        position.exit_requested_at.isoformat()
                        if position.exit_requested_at is not None
                        else None
                    ),
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
            "automation": self.automation_status(),
            "models": self.model_settings(),
        }

    async def close_position(self, position_id: str, *, actor: str) -> Any:
        """Operator-initiated close of one open position.

        Serialized with cycles and reconciles because all three read and
        write positions. Runs the deterministic exit path for that one
        position; the kill switch never blocks an exit.

        Testnet positions are held on a real exchange account. When the market
        route is not testnet, the mark comes from the testnet venue's own
        public book; the paper feed never prices a position paper does not hold.
        """

        orchestrator = self.orchestrator
        if orchestrator is None:
            raise RuntimeError("container has not been started")
        started = datetime.now(UTC)
        async with self._cycle_lock:
            async with self.ledger_lock:
                requested = await orchestrator.request_close(position_id, actor=actor)
                snapshots: list[Any] = []
                position_mode = Mode(getattr(requested, "mode", None) or Mode.PAPER.value)
                try:
                    if position_mode is Mode.TESTNET:
                        snapshots = await self._testnet_account_snapshots(
                            requested.symbol
                        )
                    else:
                        snapshots = await self.market.snapshots([requested.symbol])
                except AppError:
                    snapshots = []
                for snapshot in snapshots:
                    await self.repo.insert_market_snapshot(snapshot)
                system = await orchestrator._exit_system_snapshot()
                exits, notes = await orchestrator._manage_exits(
                    policy=None,
                    snapshot_map={snap.symbol: snap for snap in snapshots},
                    system=system,
                )
        mode = await self.modes.current_mode()
        positions = await self.repo.get_open_positions(self.account_id)
        still_open = any(position.id == position_id for position in positions)
        await self.audit.record(
            "position.manual_close",
            "position",
            position_id,
            {"actor": actor, "symbol": requested.symbol, "exits": exits},
        )
        return {
            "position_id": position_id,
            "symbol": requested.symbol,
            "closed": not still_open,
            "exits": exits,
            "open_positions": len(positions),
            "mode": mode.value,
            "notes": notes,
            "finished_at": datetime.now(UTC).isoformat(),
            "started_at": started.isoformat(),
        }

    async def _testnet_account_snapshots(self, symbol: str) -> list[Any]:
        """A mark for a testnet-held position when the active route is paper.

        The paper feed is not an exchange, so it cannot price a position the
        testnet account holds. Read the testnet venue's own ticker book
        (signed, testnet host) and derive the indicators from its klines.
        Returns an empty list when testnet credentials are absent; the exit
        path then reports the feed as unavailable instead of guessing.
        """

        account = BinanceAccountClient(
            base_url=self.settings.binance_testnet_base_url,
            api_key=(
                self.settings.binance_testnet_api_key.get_secret_value()
                if self.settings.binance_testnet_api_key
                else None
            ),
            api_secret=(
                self.settings.binance_testnet_api_secret.get_secret_value()
                if self.settings.binance_testnet_api_secret
                else None
            ),
        )
        try:
            book = await account._get("/api/v3/ticker/bookTicker", {"symbol": symbol})
            rows = await account._get(
                "/api/v3/klines", {"symbol": symbol, "interval": "1m", "limit": 240}
            )
        finally:
            await account.close()
        now = datetime.now(UTC)
        candles = []
        for row in rows if isinstance(rows, list) else []:
            close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=UTC)
            if close_time > now:
                continue
            candles.append(
                Candle(
                    open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                    close_time=close_time,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        bid = Decimal(str(book["bidPrice"]))
        ask = Decimal(str(book["askPrice"]))
        return [
            MarketSnapshot(
                symbol=symbol,
                as_of=now,
                source="binance_testnet",
                bid=bid,
                ask=ask,
                candle_close_at=candles[-1].close_time if candles else now,
                features=build_features(candles) if candles else {},
                is_synthetic=False,
            )
        ]

    async def run_cycle(self, *, refresh_policy: bool = False, wait: bool = True):
        orchestrator = self.orchestrator
        if orchestrator is None:
            raise RuntimeError("container has not been started")
        if not self._cycle_lock.locked():
            async with self._cycle_lock:
                async with self.ledger_lock:
                    return await orchestrator.run_cycle(refresh_policy=refresh_policy)
        if not wait:
            raise AppError(
                ErrorCode.CONFLICT,
                "A cycle is already running; retry when it finishes.",
            )
        async with self._cycle_lock:
            async with self.ledger_lock:
                return await orchestrator.run_cycle(refresh_policy=refresh_policy)

    async def close(self) -> None:
        for task in (self._balance_task, self._cycle_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._balance_task = None
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
