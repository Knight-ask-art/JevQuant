"""What a round trip costs, defined once.

Fees and slippage used to live in three places with three defaults: the
settings, the exchange-rule model (whose fee fields were never filled from the
venue, so the default read like a fact), and a hardcoded ``Decimal("10")`` in
the experiment runner. An analysis that disagrees with the execution path about
the cost of a trade cannot be used to judge that path, so both now read the
same model.

Two things are deliberately separate:

- **Modelled cost** (this module) is the assumption the system reasons with.
  It is conservative by construction and never claims to be measured.
- **Realised cost** (``services/cost_report.py``) is what the venue actually
  charged, read back from the fills. Comparing the two is how the assumption
  gets corrected -- by a person, on evidence, not by silently drifting.

Slippage here means impact *beyond the quoted touch*: the entry already pays
the ask and the exit already pays the bid, so the spread is inside the prices
and must not be charged twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .money import BPS, ONE, ZERO, dec

#: Mirrors the Settings defaults, for callers holding a partial settings object.
DEFAULT_FEE_BPS = Decimal("10")
DEFAULT_ENTRY_SLIPPAGE_BPS = Decimal("2")
DEFAULT_EXIT_SLIPPAGE_BPS = Decimal("8")


@dataclass(frozen=True)
class CostModel:
    """The assumed cost of one entry and one exit, in basis points."""

    fee_bps: Decimal = DEFAULT_FEE_BPS
    entry_slippage_bps: Decimal = DEFAULT_ENTRY_SLIPPAGE_BPS
    exit_slippage_bps: Decimal = DEFAULT_EXIT_SLIPPAGE_BPS

    @classmethod
    def from_settings(cls, settings: Any) -> CostModel:
        return cls(
            fee_bps=dec(getattr(settings, "fee_bps", DEFAULT_FEE_BPS)),
            entry_slippage_bps=dec(
                getattr(settings, "slippage_bps", DEFAULT_ENTRY_SLIPPAGE_BPS)
            ),
            exit_slippage_bps=dec(
                getattr(settings, "exit_slippage_bps", DEFAULT_EXIT_SLIPPAGE_BPS)
            ),
        )

    @property
    def round_trip_bps(self) -> Decimal:
        """Both sides' fees plus the slippage paid moving in and out."""

        return self.fee_bps * Decimal("2") + self.entry_slippage_bps + self.exit_slippage_bps

    def entry_price(self, ask: Decimal) -> Decimal:
        """The price a buy is assumed to fill at, before tick rounding."""

        return ask * (ONE + self.entry_slippage_bps / BPS)

    def exit_price(self, price: Decimal) -> Decimal:
        """The price a sell is assumed to fill at, before tick rounding."""

        return price * (ONE - self.exit_slippage_bps / BPS)

    def fee_amount(self, notional: Decimal) -> Decimal:
        return notional * self.fee_bps / BPS

    def as_dict(self) -> dict[str, str]:
        return {
            "fee_bps": str(self.fee_bps),
            "entry_slippage_bps": str(self.entry_slippage_bps),
            "exit_slippage_bps": str(self.exit_slippage_bps),
            "round_trip_bps": str(self.round_trip_bps),
        }


@dataclass(frozen=True)
class Commission:
    """A fee the venue charged, and what it is worth in the quote asset.

    ``quote_value`` is ``None`` when the fee was paid in an asset the system
    has no price for (BNB, typically). That is recorded rather than rounded to
    zero: a fee that cannot be priced is a missing number, not a free trade.
    """

    asset: str
    amount: Decimal
    quote_value: Decimal | None

    @property
    def priced(self) -> bool:
        return self.quote_value is not None


def quote_commission(
    *,
    amount: Decimal,
    asset: str,
    price: Decimal,
    symbol: str = "",
    base_asset: str = "",
    quote_asset: str = "",
) -> Commission:
    """Price a venue commission in the quote asset.

    Binance spot charges a buy in the **base** asset (you receive less of what
    you bought) and a sell in the **quote** asset. Reading only the quote case
    records every entry fee as zero, which makes realised P&L better than it
    is -- exactly the error that makes a losing strategy look viable.

    The asset roles come from the exchange rules when the caller has them.
    Reconciliation only has the symbol, so the pair is read off it instead: a
    spot symbol is ``BASE + QUOTE``, which is enough to tell the two apart
    without a hardcoded asset list. An asset that is neither (BNB fees) stays
    unpriced.
    """

    amount = dec(amount)
    code = (asset or "").upper()
    quote = (quote_asset or "").upper()
    base = (base_asset or "").upper()
    pair = (symbol or "").upper()

    if amount <= ZERO:
        return Commission(asset=code or quote or base, amount=amount, quote_value=ZERO)
    is_quote = code == quote if quote else bool(code and pair.endswith(code))
    is_base = code == base if base else bool(code and pair.startswith(code))
    if is_quote:
        return Commission(asset=code, amount=amount, quote_value=amount)
    if is_base:
        # Priced at the fill itself: the fee was taken out of the base asset
        # that changed hands at exactly that price.
        return Commission(asset=code, amount=amount, quote_value=amount * price)
    return Commission(asset=code, amount=amount, quote_value=None)
