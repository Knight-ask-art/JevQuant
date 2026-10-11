"""
Configuration management and safety preflight gates for JevQuant.
"""

from decimal import Decimal
import os
from pathlib import Path
from typing import Literal, Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Automatically load .env if present
load_dotenv()

TradingMode = Literal["paper", "testnet", "live"]


class AppConfig(BaseModel):
    trading_mode: TradingMode = Field(default="paper")
    enable_live_trading: bool = Field(default=False)
    exchange_id: str = Field(default="binance")
    symbol: str = Field(default="BTC/USDT")
    timeframe: str = Field(default="1h")

    # Capital settings in USDT
    paper_initial_balance_usdt: Decimal = Field(default=Decimal("7.00"))
    live_initial_capital_usdt: Optional[Decimal] = Field(default=None)

    # API Keys & Secrets (Must not be logged)
    typesafe_api_key: Optional[str] = Field(default=None)
    binance_api_key: Optional[str] = Field(default=None)
    binance_api_secret: Optional[str] = Field(default=None)
    binance_testnet_api_key: Optional[str] = Field(default=None)
    binance_testnet_api_secret: Optional[str] = Field(default=None)

    # Jev Settings
    jev_model: str = Field(default="jev-latest")
    jev_max_monthly_cost_usd: Decimal = Field(default=Decimal("0.50"))

    # Database
    db_path: Path = Field(default=Path("data/jevquant.db"))

    @field_validator("trading_mode", mode="before")
    @classmethod
    def normalize_mode(cls, v: str) -> str:
        return str(v).lower().strip()

    @field_validator("paper_initial_balance_usdt", "live_initial_capital_usdt", mode="before")
    @classmethod
    def parse_decimal(cls, v):
        if v is None or v == "":
            return None
        return Decimal(str(v))

    def validate_safety_gates(self, cli_confirm_live: bool = False) -> None:
        """
        Validates safety invariants per JevQuant.spec before any engine runs.
        """
        if self.trading_mode == "live":
            if not self.enable_live_trading:
                raise PermissionError("Live trading is forbidden when ENABLE_LIVE_TRADING is false.")
            if not cli_confirm_live:
                raise PermissionError("Live trading requires explicit CLI --confirm-live flag.")
            if not self.binance_api_key or not self.binance_api_secret:
                raise ValueError("Binance Live API Key and Secret are required for live mode.")
            if self.live_initial_capital_usdt is None or self.live_initial_capital_usdt <= 0:
                raise ValueError("LIVE_INITIAL_CAPITAL_USDT must be explicitly set and positive.")
            if self.live_initial_capital_usdt > Decimal("10.00"):
                # Safety cap: ~50 CNY equivalent is roughly ~7-8 USDT, capping at 10 USDT max
                raise ValueError("LIVE_INITIAL_CAPITAL_USDT exceeds safety cap of 10 USDT (~70 CNY).")

        if self.trading_mode == "testnet":
            if not self.binance_testnet_api_key or not self.binance_testnet_api_secret:
                raise ValueError("Binance Testnet API Key and Secret are required for testnet mode.")


def load_config_from_env() -> AppConfig:
    """Loads configuration from environment variables."""
    return AppConfig(
        trading_mode=os.getenv("TRADING_MODE", "paper"),
        enable_live_trading=os.getenv("ENABLE_LIVE_TRADING", "false").lower() in ("true", "1", "yes"),
        exchange_id=os.getenv("EXCHANGE", "binance"),
        symbol=os.getenv("SYMBOL", "BTC/USDT"),
        timeframe=os.getenv("TIMEFRAME", "1h"),
        paper_initial_balance_usdt=os.getenv("PAPER_INITIAL_BALANCE_USDT", "7.00"),
        live_initial_capital_usdt=os.getenv("LIVE_INITIAL_CAPITAL_USDT") or None,
        typesafe_api_key=os.getenv("TYPESAFE_API_KEY") or None,
        binance_api_key=os.getenv("BINANCE_API_KEY") or None,
        binance_api_secret=os.getenv("BINANCE_API_SECRET") or None,
        binance_testnet_api_key=os.getenv("BINANCE_TESTNET_API_KEY") or None,
        binance_testnet_api_secret=os.getenv("BINANCE_TESTNET_API_SECRET") or None,
        jev_model=os.getenv("JEV_MODEL", "jev-latest"),
        jev_max_monthly_cost_usd=os.getenv("JEV_MAX_MONTHLY_COST_USD", "0.50"),
        db_path=Path(os.getenv("DB_PATH", "data/jevquant.db")),
    )
