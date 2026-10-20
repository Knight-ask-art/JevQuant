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
    # Fernet key generated once, kept outside the database; required for UI key persistence.
    credential_encryption_key: SecretStr | None = None

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
    # Over-trading protection (Freqtrade-style "guard"): within
    # overtrade_window_seconds a symbol may enter at most
    # overtrade_max_entries times; the next entry waits for the oldest to age
    # out. This bounds churn no matter what cooldown the policy proposes.
    overtrade_window_seconds: int = Field(default=3600, ge=0, le=86_400)
    overtrade_max_entries: int = Field(default=3, ge=1, le=100)
    # Paid-call budgets. The spec's rate policy caps how often the paid
    # providers may be called; the counters are rebuilt from the ledger, so
    # they survive a restart. Counts are exact. USD caps are only enforced
    # once a price is configured for that provider: an unpriced provider
    # reports cost 0, and a cap that can never be reached is worse than none.
    # 0 disables the individual limit.
    jev_max_calls_per_utc_day: int = Field(default=30, ge=0, le=100_000)
    # Jev is asked at most once per closed 1h candle (the spec's rate
    # policy). A 60-second cycle re-asking the same candle spends the daily
    # budget on identical evidence, so the call itself is paced here, server
    # side: a policy (or the model writing it) cannot loosen it, and exits do
    # not need Jev. 0 disables pacing.
    jev_min_call_interval_seconds: int = Field(default=3600, ge=0, le=86_400)
    jev_max_calls_per_utc_month: int = Field(default=900, ge=0, le=3_000_000)
    jev_max_monthly_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    jev_input_price_per_mtok_usd: Decimal = Field(default=Decimal("0"), ge=0)
    jev_output_price_per_mtok_usd: Decimal = Field(default=Decimal("0"), ge=0)
    model_max_tokens_per_utc_month: int = Field(default=3_000_000, ge=0)
    model_max_monthly_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    model_input_price_per_mtok_usd: Decimal = Field(default=Decimal("0"), ge=0)
    # Cached prompt tokens are usually discounted; 0 prices them as input.
    model_cached_input_price_per_mtok_usd: Decimal = Field(default=Decimal("0"), ge=0)
    model_output_price_per_mtok_usd: Decimal = Field(default=Decimal("0"), ge=0)

    # Exit feasibility is judged against this adverse move, not just the
    # stop: a position that cannot be sold after a 3% drop plus fees and
    # slippage must not be opened.
    exit_stress_drawdown_bps: Decimal = Field(default=Decimal("300"), ge=0, le=5_000)

    min_choice_probability: float = 0.5
    min_policy_fit: float = 0.5
    min_quality_score: float = 0.0

    paper_starting_equity: Decimal = Decimal("10000")
    # Share of available quote cash one entry may spend. Server side, so a
    # policy cannot size a position past what the account can lose on one
    # trade; on a small account it also decides whether an entry can clear the
    # exchange minimum at all (a 14 USDT account at 0.25 is 3.5, below
    # Binance's 5).
    entry_budget_fraction: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
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
    # Strategy-agent loop bounds. The model may call read-only tools before
    # writing a policy; these cap how long one authoring turn may take.
    agent_max_turns: int = Field(default=6, ge=1, le=12)
    agent_budget_seconds: int = Field(default=90, ge=10, le=600)

    opencode_go_enabled: bool = False
    opencode_go_base_url: str | None = None
    opencode_go_api_key: SecretStr | None = None
    opencode_go_model: str | None = None

    jev_enabled: bool = False
    jev_base_url: str | None = None
    jev_api_key: SecretStr | None = None
    jev_model: str = "jev-1.13"
    jev_path: str = "/v1/decisions"

    hummingbot_base_url: str | None = None
    hummingbot_api_key: SecretStr | None = None

    synthetic_market: bool = True
    demo_mode: bool = True

    # Background cycle scheduler. 0 disables it; cycles are then driven by the
    # CLI or POST /api/v1/demo/cycle. A non-zero value is auto-enabled at
    # startup, so an unattended host never cycles silently while a human
    # expects it to. A runtime switch (PUT /settings/automation) is still
    # persisted in system_state and restored on restart.
    auto_cycle_seconds: int = Field(default=0, ge=0, le=3600)
    # A cycle whose candle has not closed since the last cycle has no new
    # market evidence. On synthetic data replaying the same candle produces
    # the same signal every minute; the default skips those cycles. Set false
    # for demos that show a signal on every press.
    cycle_requires_new_candle: bool = True

    # Exchange-account balance sync (testnet/live venues only). 0 disables the
    # scheduler; POST /api/v1/execution/balances/sync still runs on demand.
    balance_sync_seconds: int = Field(default=0, ge=0, le=86_400)
    # When a balance read leaves tolerance, run one reconciliation for that
    # route so a fill the ledger missed can settle itself. The cooldown is per
    # route and starts even on failure: a persistent difference must not turn
    # every sync into a burst of venue calls.
    balance_auto_reconcile: bool = True
    balance_reconcile_cooldown_seconds: int = Field(default=300, ge=0, le=86_400)

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
                "entry_budget_fraction": str(self.entry_budget_fraction),
                "allowed_symbols": self.allowed_symbols,
            },
            "budgets": {
                "jev_max_calls_per_utc_day": self.jev_max_calls_per_utc_day,
                "jev_max_calls_per_utc_month": self.jev_max_calls_per_utc_month,
                "jev_max_monthly_cost_usd": str(self.jev_max_monthly_cost_usd),
                "jev_priced": bool(
                    self.jev_input_price_per_mtok_usd
                    or self.jev_output_price_per_mtok_usd
                ),
                "jev_min_call_interval_seconds": self.jev_min_call_interval_seconds,
                "model_max_tokens_per_utc_month": self.model_max_tokens_per_utc_month,
                "model_max_monthly_cost_usd": str(self.model_max_monthly_cost_usd),
                "model_priced": bool(
                    self.model_input_price_per_mtok_usd
                    or self.model_output_price_per_mtok_usd
                ),
            },
        }


def get_settings() -> Settings:
    return Settings()
