"""Server-side mode manager and the Live arming handshake.

Live is not a browser toggle.  It is a two-step, server-side process:
preflight (configuration, keys, permissions, dependencies, feed) -> a
short-lived one-time token -> explicit confirmation + TOTP -> atomic arm.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from ..domain.enums import ArmingStatus, ErrorCode, Mode
from ..domain.errors import AppError, ModeNotArmedError
from ..domain.market import SystemSnapshot
from ..storage.repository import Repository
from .audit import AuditService
from .events import EventBroker
from .market_service import MarketService
from .totp import verify_totp

MODE_KEY = "mode"
ARMING_STATUS_KEY = "arming_status"
ARMED_AT_KEY = "armed_at"
ARMING_EXPIRES_KEY = "arming_expires_at"
KILL_SWITCH_KEY = "kill_switch"
LAST_REASON_KEY = "last_reason"

CONFIRMATION_PHRASE = "ARM LIVE TRADING"
PREFLIGHT_TOKEN_TTL_SECONDS = 300


class ModeManager:
    def __init__(
        self,
        *,
        repo: Repository,
        settings,
        audit: AuditService,
        broker: EventBroker,
        market: MarketService,
        hummingbot,
        binance_account,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.audit = audit
        self.broker = broker
        self.market = market
        self.hummingbot = hummingbot
        self.binance_account = binance_account
        self.account_id = ""

    async def initialize(self) -> None:
        if await self.repo.get_state(MODE_KEY) is None:
            await self.repo.set_state(MODE_KEY, self.settings.trading_mode.value)
        if await self.repo.get_state(ARMING_STATUS_KEY) is None:
            await self.repo.set_state(ARMING_STATUS_KEY, ArmingStatus.DISARMED.value)
        if await self.repo.get_state(KILL_SWITCH_KEY) is None:
            await self.repo.set_state(KILL_SWITCH_KEY, False)
        # A restart always returns Live to disarmed. Existing positions keep
        # their deterministic exits, but no new entries can occur.
        if await self.repo.get_state(MODE_KEY) == Mode.LIVE.value:
            await self.repo.set_state(MODE_KEY, Mode.PAPER.value)
            await self.repo.set_state(ARMING_STATUS_KEY, ArmingStatus.DISARMED.value)
            await self.repo.set_state(LAST_REASON_KEY, "restart_reset")

    async def current_mode(self) -> Mode:
        value = await self.repo.get_state(MODE_KEY, Mode.PAPER.value)
        return Mode(value)

    async def kill_switch_active(self) -> bool:
        return bool(await self.repo.get_state(KILL_SWITCH_KEY, False))

    async def is_live_armed(self) -> bool:
        if await self.current_mode() is not Mode.LIVE:
            return False
        if await self.kill_switch_active():
            return False
        status = await self.repo.get_state(ARMING_STATUS_KEY, ArmingStatus.DISARMED.value)
        if status != ArmingStatus.ARMED.value:
            return False
        expires_raw = await self.repo.get_state(ARMING_EXPIRES_KEY)
        if not expires_raw:
            return False
        try:
            expires = datetime.fromisoformat(expires_raw)
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > datetime.now(UTC)

    async def system_snapshot(
        self,
        *,
        feed_healthy: bool,
        execution_healthy: bool,
        policy_fresh: bool,
    ) -> SystemSnapshot:
        mode = await self.current_mode()
        return SystemSnapshot(
            mode=mode.value,
            live_armed=await self.is_live_armed(),
            kill_switch=await self.kill_switch_active(),
            feed_healthy=feed_healthy,
            execution_healthy=execution_healthy,
            policy_fresh=policy_fresh,
        )

    async def status(self) -> dict[str, Any]:
        mode = await self.current_mode()
        return {
            "mode": mode.value,
            "live_armed": await self.is_live_armed(),
            "arming_status": await self.repo.get_state(
                ARMING_STATUS_KEY, ArmingStatus.DISARMED.value
            ),
            "armed_at": await self.repo.get_state(ARMED_AT_KEY),
            "arming_expires_at": await self.repo.get_state(ARMING_EXPIRES_KEY),
            "kill_switch": await self.kill_switch_active(),
            "last_reason": await self.repo.get_state(LAST_REASON_KEY),
            "live_config_allowed": self.settings.live_config_allowed(),
            "execution_backend": self.settings.execution_backend,
        }

    async def set_mode(self, mode: Mode, *, actor: str) -> dict[str, Any]:
        if mode is Mode.LIVE:
            raise AppError(
                ErrorCode.MODE_NOT_ARMED,
                "Live mode cannot be set directly; use the arming handshake.",
            )
        await self._assert_no_live_positions("changing mode")
        if mode is Mode.TESTNET and not (
            self.settings.binance_testnet_api_key
            and self.settings.binance_testnet_api_secret
        ):
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Testnet mode requires Binance Testnet credentials.",
            )
        await self.repo.set_state(MODE_KEY, mode.value)
        await self.repo.set_state(ARMING_STATUS_KEY, ArmingStatus.DISARMED.value)
        await self.repo.set_state(LAST_REASON_KEY, f"mode_set:{mode.value}")
        await self.audit.record(
            "mode.changed", "system", None, {"mode": mode.value, "actor": actor}
        )
        await self.broker.publish(
            "system.events", {"event": "mode.changed", "mode": mode.value}
        )
        return await self.status()

    async def preflight_live(self, *, actor: str) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append({"name": name, "passed": bool(passed), "detail": detail})

        check(
            "env_trading_mode_live",
            self.settings.trading_mode is Mode.LIVE,
            f"trading_mode={self.settings.trading_mode.value}",
        )
        check(
            "env_enable_live_trading",
            self.settings.enable_live_trading,
            f"enable_live_trading={self.settings.enable_live_trading}",
        )
        check(
            "execution_backend_hummingbot",
            self.settings.execution_backend == "hummingbot",
            f"execution_backend={self.settings.execution_backend}",
        )
        check("mfa_configured", bool(self.settings.mfa_totp_secret), "TOTP secret required")
        check(
            "binance_live_credentials",
            bool(self.settings.binance_api_key and self.settings.binance_api_secret),
            "trade-only, withdrawal-disabled key required",
        )
        check(
            "kill_switch_off",
            not await self.kill_switch_active(),
            "kill switch must be off",
        )
        check(
            "redis_healthy",
            not self.settings.redis_url or self.broker.redis_healthy,
            "" if self.broker.redis_healthy else "Redis configured but unhealthy",
        )

        try:
            await self.repo.get_state(MODE_KEY)
            check("database", True, "state store reachable")
        except Exception as exc:  # noqa: BLE001
            check("database", False, type(exc).__name__)

        feed = await self.market.health()
        check("market_feed", bool(feed.get("healthy")), str(feed.get("detail", "")))
        hummingbot = await self.hummingbot.health()
        check(
            "hummingbot",
            bool(hummingbot.get("healthy")),
            str(hummingbot.get("error", "")),
        )

        if self.settings.binance_api_key and self.settings.binance_api_secret:
            permission = await self.binance_account.preflight(
                require_ip_restrict=self.settings.require_ip_restrict,
                require_withdrawals_disabled=self.settings.require_withdrawals_disabled,
            )
            for item in permission["checks"]:
                if item["name"] == "permission_endpoint":
                    # Transport/location failure (e.g. HTTP 451). Report it, but
                    # the key permissions in *this* response are simply unknown.
                    check(
                        "binance_permissions",
                        False,
                        "Binance API unreachable: " + str(item.get("detail", ""))[:200],
                    )
                    continue
                check(f"binance_{item['name']}", item["passed"], item["detail"])
        else:
            check("binance_permissions", False, "credentials missing")

        passed = all(item["passed"] for item in checks)
        token: str | None = None
        if passed:
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            await self.repo.create_arming_token(
                token_hash,
                datetime.now(UTC) + timedelta(seconds=PREFLIGHT_TOKEN_TTL_SECONDS),
                {"checks": checks, "actor": actor},
            )
            await self.repo.set_state(
                ARMING_STATUS_KEY, ArmingStatus.PREFLIGHT_RUNNING.value
            )
        else:
            await self.repo.set_state(
                ARMING_STATUS_KEY, ArmingStatus.PREFLIGHT_FAILED.value
            )
            await self.repo.set_state(LAST_REASON_KEY, "live_preflight_failed")
        await self.audit.record(
            "live.preflight",
            "system",
            None,
            {"passed": passed, "checks": checks, "actor": actor},
        )
        return {"passed": passed, "checks": checks, "arming_token": token}

    async def _assert_no_live_positions(self, action: str) -> None:
        """Refuse a route change while a live position would lose its exit path.

        After a restart the mode resets to paper, so without this guard real
        exchange positions would be "closed" on the paper ledger while still
        open on Binance.
        """

        if not self.account_id:
            return
        positions = await self.repo.get_open_positions(self.account_id)
        live = sorted({position.symbol for position in positions if position.mode == Mode.LIVE.value})
        if live:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Cannot continue {action} while live positions are open "
                f"({', '.join(live)}). Close them from the live route first.",
                details={"live_symbols": live},
            )

    async def arm_live(
        self,
        *,
        arming_token: str,
        confirmation: str,
        totp_code: str,
        actor: str,
    ) -> dict[str, Any]:
        if not self.settings.live_config_allowed():
            raise ModeNotArmedError(
                "Live arming requires TRADING_MODE=live and ENABLE_LIVE_TRADING=true."
            )
        await self._assert_no_live_positions("live arming")
        if confirmation != CONFIRMATION_PHRASE:
            raise AppError(ErrorCode.FORBIDDEN, "Live confirmation phrase did not match.")
        secret = self.settings.mfa_totp_secret
        if secret is None or not verify_totp(secret.get_secret_value(), totp_code):
            await self.audit.record(
                "live.arm_denied", "system", None, {"reason": "mfa_failed", "actor": actor}
            )
            raise AppError(ErrorCode.FORBIDDEN, "TOTP verification failed.")
        token_hash = hashlib.sha256(arming_token.encode("utf-8")).hexdigest()
        token_row = await self.repo.consume_arming_token(token_hash)
        if token_row is None:
            raise ModeNotArmedError("Arming token is missing, expired or already used.")
        if await self.kill_switch_active():
            raise AppError(ErrorCode.KILL_SWITCH_ACTIVE, "Kill switch must be reset first.")
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self.settings.live_arming_ttl_seconds)
        await self.repo.set_state(MODE_KEY, Mode.LIVE.value)
        await self.repo.set_state(ARMING_STATUS_KEY, ArmingStatus.ARMED.value)
        await self.repo.set_state(ARMED_AT_KEY, now.isoformat())
        await self.repo.set_state(ARMING_EXPIRES_KEY, expires.isoformat())
        await self.repo.set_state(LAST_REASON_KEY, f"armed_by:{actor}")
        await self.audit.record(
            "live.armed",
            "system",
            None,
            {"actor": actor, "expires_at": expires.isoformat()},
        )
        await self.broker.publish(
            "system.events",
            {"event": "live.armed", "expires_at": expires.isoformat()},
        )
        return await self.status()

    async def disarm(self, *, reason: str, actor: str) -> dict[str, Any]:
        # Keep the current mode: exits of existing positions must continue to
        # route through the adapter that owns them. Only new entries are blocked.
        await self.repo.set_state(ARMING_STATUS_KEY, ArmingStatus.DISARMED.value)
        await self.repo.set_state(ARMING_EXPIRES_KEY, None)
        await self.repo.set_state(LAST_REASON_KEY, reason)
        await self.audit.record(
            "live.disarmed", "system", None, {"reason": reason, "actor": actor}
        )
        await self.broker.publish(
            "system.events", {"event": "live.disarmed", "reason": reason}
        )
        return await self.status()

    async def kill(self, *, reason: str, actor: str) -> dict[str, Any]:
        # The kill switch blocks new entries but deliberately leaves the mode
        # in place so deterministic exits keep their execution path.
        await self.repo.set_state(KILL_SWITCH_KEY, True)
        await self.repo.set_state(ARMING_STATUS_KEY, ArmingStatus.DISARMED.value)
        await self.repo.set_state(ARMING_EXPIRES_KEY, None)
        await self.repo.set_state(LAST_REASON_KEY, f"kill:{reason}")
        await self.audit.record(
            "system.kill_switch", "system", None, {"reason": reason, "actor": actor}
        )
        await self.broker.publish(
            "system.events", {"event": "kill_switch", "reason": reason}
        )
        return await self.status()

    async def reset_kill_switch(self, *, actor: str) -> dict[str, Any]:
        await self.repo.set_state(KILL_SWITCH_KEY, False)
        await self.repo.set_state(LAST_REASON_KEY, f"kill_reset_by:{actor}")
        await self.audit.record(
            "system.kill_switch_reset", "system", None, {"actor": actor}
        )
        return await self.status()
