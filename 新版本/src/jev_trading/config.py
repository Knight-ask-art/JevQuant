"""Environment configuration and the hard server risk envelope."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.enums import Mode
from .domain.policy import ServerLimits


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JEV_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "jev-trading"

    database_url: str = "sqlite+aiosqlite:///./data/jev.db"
    redis_url: str | None = None
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    execution_backend: Literal["paper", "shadow", "hummingbot"] = "paper"
    trading_mode: Mode = Mode.PAPER
    enable_live_trading: bool = False

    auth_disabled: bool = True
    admin_api_key: SecretStr = SecretStr("change-me-in-production")
    internal_execution_key: SecretStr = SecretStr("change-me-internal")

    # Hard risk envelope
    max_position_notional_usdt: Decimal = Decimal("500")
    max_gross_exposure_pct: Decimal = Decimal("20")
    max_daily_loss_pct: Decimal = Decimal("2")
    max_positions: int = 3
    max_drawdown_pct: Decimal = Decimal("5")
    max_policy_ttl_seconds: int = 7200
    allowed_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    market_staleness_ms: int = 5000
    # Maximum age of a cached exchange filter snapshot used to admit an entry.
    # Older values force a refresh; a refresh failure blocks the entry.
    entry_filter_ttl_seconds: int = Field(default=60, ge=1, le=600)
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("2")
    exit_slippage_bps: Decimal = Decimal("8")
    min_stop_loss_bps: Decimal = Decimal("20")
    max_stop_loss_bps: Decimal = Decimal("300")
    min_entry_cooldown_seconds: int = 60
    min_choice_probability: float = 0.5
    min_policy_fit: float = 0.5
    min_quality_score: float = 0.0

    paper_starting_equity: Decimal = Decimal("10000")
    paper_seed: int = 42

    binance_base_url: str = "https://api.binance.com"
    binance_testnet_base_url: str = "https://testnet.binance.vision"
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None
    require_ip_restrict: bool = True
    require_withdrawals_disabled: bool = True

    mfa_totp_secret: SecretStr | None = None
    live_arming_ttl_seconds: int = 43_200

    opencode_console_enabled: bool = False
    opencode_console_base_url: str | None = None
    opencode_console_api_key: SecretStr | None = None
    opencode_console_model: str | None = None
    opencode_console_protocol: Literal["openai", "anthropic"] = "openai"

    opencode_go_enabled: bool = False
    opencode_go_base_url: str | None = None
    opencode_go_api_key: SecretStr | None = None
    opencode_go_model: str | None = None

    jev_enabled: bool = False
    jev_base_url: str | None = None
    jev_api_key: SecretStr | None = None
    jev_model: str = "jev-1.13"
    jev_path: str = "/v1/inference"

    hummingbot_base_url: str | None = None
    hummingbot_api_key: SecretStr | None = None

    synthetic_market: bool = True
    demo_mode: bool = True

    # Background cycle scheduler. 0 disables it; cycles are then driven by the
    # CLI or POST /api/v1/demo/cycle. A non-zero value is the wall-clock
    # interval between cycle starts.
    auto_cycle_seconds: int = Field(default=0, ge=0, le=3600)

    @field_validator("allowed_symbols")
    @classmethod
    def _normalize_symbols(cls, value: list[str]) -> list[str]:
        normalized = []
        for symbol in value:
            upper = symbol.strip().upper()
            if upper and upper not in normalized:
                normalized.append(upper)
        if not normalized:
            raise ValueError("JEV_ALLOWED_SYMBOLS cannot be empty")
        return normalized

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app_env == "prod":
            if self.auth_disabled:
                raise ValueError("JEV_AUTH_DISABLED must be false in prod")
            if self.admin_api_key.get_secret_value() == "change-me-in-production":
                raise ValueError("JEV_ADMIN_API_KEY must be changed in prod")
            if self.internal_execution_key.get_secret_value() == "change-me-internal":
                raise ValueError("JEV_INTERNAL_EXECUTION_KEY must be changed in prod")
            if self.synthetic_market:
                raise ValueError("JEV_SYNTHETIC_MARKET must be false in prod")
            if self.trading_mode is Mode.LIVE and not self.enable_live_trading:
                raise ValueError("live mode requires JEV_ENABLE_LIVE_TRADING=true")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"

    @property
    def server_limits(self) -> ServerLimits:
        return ServerLimits(
            max_position_notional_usdt=self.max_position_notional_usdt,
            max_gross_exposure_pct=self.max_gross_exposure_pct,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_positions=self.max_positions,
            max_drawdown_pct=self.max_drawdown_pct,
            max_policy_ttl_seconds=self.max_policy_ttl_seconds,
            allowed_symbols=frozenset(self.allowed_symbols),
            max_spread_bps=Decimal("5"),
            max_slippage_bps=Decimal("15"),
            min_stop_loss_bps=self.min_stop_loss_bps,
            max_stop_loss_bps=self.max_stop_loss_bps,
            max_holding_seconds=86_400,
            min_entry_cooldown_seconds=self.min_entry_cooldown_seconds,
            min_choice_probability=self.min_choice_probability,
            min_policy_fit=self.min_policy_fit,
            min_quality_score=self.min_quality_score,
        )

    def live_config_allowed(self) -> bool:
        return self.trading_mode is Mode.LIVE and self.enable_live_trading

    def redacted_summary(self) -> dict[str, object]:
        """Safe to log or expose: never includes secret values."""
        return {
            "app_env": self.app_env,
            "trading_mode": self.trading_mode.value,
            "execution_backend": self.execution_backend,
            "enable_live_trading": self.enable_live_trading,
            "auth_disabled": self.auth_disabled,
            "database_url_scheme": self.database_url.split(":", 1)[0],
            "redis_configured": bool(self.redis_url),
            "synthetic_market": self.synthetic_market,
            "demo_mode": self.demo_mode,
            "opencode_console_configured": bool(
                self.opencode_console_api_key and self.opencode_console_base_url
            ),
            "opencode_go_configured": bool(
                self.opencode_go_api_key and self.opencode_go_base_url
            ),
            "jev_configured": bool(self.jev_api_key and self.jev_base_url),
            "hummingbot_configured": bool(
                self.hummingbot_api_key and self.hummingbot_base_url
            ),
            "binance_live_credentials_configured": bool(
                self.binance_api_key and self.binance_api_secret
            ),
            "binance_testnet_credentials_configured": bool(
                self.binance_testnet_api_key and self.binance_testnet_api_secret
            ),
            "mfa_configured": bool(self.mfa_totp_secret),
            "server_limits": {
                "max_position_notional_usdt": str(self.max_position_notional_usdt),
                "max_gross_exposure_pct": str(self.max_gross_exposure_pct),
                "max_daily_loss_pct": str(self.max_daily_loss_pct),
                "max_positions": self.max_positions,
                "max_drawdown_pct": str(self.max_drawdown_pct),
                "allowed_symbols": self.allowed_symbols,
            },
        }


def get_settings() -> Settings:
    return Settings()
