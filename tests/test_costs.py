"""The cost model: one definition, and the bridge to what was actually paid."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from jev_trading.domain.costs import CostModel, quote_commission
from jev_trading.services.cost_report import CostReport


def fill(
    *,
    price: str = "100",
    qty: str = "1",
    fee_quote: str = "0.1",
    fee_amount: str | None = None,
    fee_asset: str = "USDT",
    side: str = "BUY",
    intent_type: str = "OPEN",
    mode: str = "testnet",
    reference: str | None = "100",
) -> SimpleNamespace:
    return SimpleNamespace(
        qty=Decimal(qty),
        price=Decimal(price),
        fee_quote=Decimal(fee_quote),
        fee_amount=Decimal(fee_amount if fee_amount is not None else fee_quote),
        fee_asset=fee_asset,
        side=side,
        intent_type=intent_type,
        mode=mode,
        reference_price=None if reference is None else Decimal(reference),
    )


# -- the model ---------------------------------------------------------------
def test_round_trip_is_both_fees_plus_both_slippages():
    model = CostModel(
        fee_bps=Decimal("10"),
        entry_slippage_bps=Decimal("2"),
        exit_slippage_bps=Decimal("8"),
    )
    assert model.round_trip_bps == Decimal("30")


def test_a_partial_settings_stub_still_builds_a_model():
    """Tests build settings objects with only the fields their path touches."""

    model = CostModel.from_settings(SimpleNamespace(fee_bps=Decimal("7.5")))
    assert model.fee_bps == Decimal("7.5")
    assert model.entry_slippage_bps == Decimal("2")
    assert model.exit_slippage_bps == Decimal("8")


def test_entry_and_exit_prices_pay_their_own_assumption():
    model = CostModel(
        fee_bps=Decimal("10"),
        entry_slippage_bps=Decimal("2"),
        exit_slippage_bps=Decimal("8"),
    )
    assert model.entry_price(Decimal("100")) == Decimal("100.02")
    assert model.exit_price(Decimal("100")) == Decimal("99.92")


# -- commission pricing ------------------------------------------------------
def test_a_fee_taken_in_the_quote_asset_is_already_priced():
    fee = quote_commission(
        amount=Decimal("0.25"), asset="USDT", price=Decimal("60000"), symbol="BTCUSDT"
    )
    assert fee.quote_value == Decimal("0.25")
    assert fee.priced is True


def test_a_spot_buy_fee_in_the_base_asset_is_priced_at_the_fill():
    """Binance takes a buy's commission out of the base asset.

    Reading only the quote case recorded this as zero, which made realised
    P&L better than it was by exactly one side of the round trip.
    """

    fee = quote_commission(
        amount=Decimal("0.001"),
        asset="BTC",
        price=Decimal("60000"),
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )

    assert fee.asset == "BTC"
    assert fee.amount == Decimal("0.001")
    assert fee.quote_value == Decimal("60.000")


def test_the_symbol_tells_the_assets_apart_when_the_rules_are_not_at_hand():
    """Reconciliation only has the symbol; a spot pair is BASE + QUOTE."""

    base = quote_commission(amount=Decimal("2"), asset="ETH", price=Decimal("100"), symbol="ETHUSDT")
    quote = quote_commission(amount=Decimal("2"), asset="USDT", price=Decimal("100"), symbol="ETHUSDT")

    assert base.quote_value == Decimal("200")
    assert quote.quote_value == Decimal("2")


def test_a_fee_in_an_unpriced_asset_is_kept_but_not_invented():
    fee = quote_commission(
        amount=Decimal("0.01"), asset="BNB", price=Decimal("60000"), symbol="BTCUSDT"
    )
    assert fee.priced is False
    assert fee.quote_value is None
    assert fee.amount == Decimal("0.01")


def test_a_zero_commission_is_zero_rather_than_unknown():
    fee = quote_commission(
        amount=Decimal("0"), asset="", price=Decimal("1"), symbol="BTCUSDT"
    )
    assert fee.quote_value == Decimal("0")


# -- the report --------------------------------------------------------------
def test_a_fill_that_paid_in_the_base_asset_still_shows_its_cost():
    """Regression: the report used to read such a fill as fee-free."""

    # 0.6 USDT of commission on 50 USDT of notional, charged in BTC.
    report = CostReport(
        model=CostModel(),
        fills=[fill(fee_quote="0.6", fee_amount="0.0001", fee_asset="BTC", qty="0.5")],
    )

    assert report.fee_report()["median_fee_bps"] == pytest.approx(120.0, abs=0.01)


def test_unpriced_fees_are_reported_not_counted_as_free():
    report = CostReport(
        model=CostModel(),
        fills=[fill(fee_quote="0", fee_amount="0.001", fee_asset="BNB")] * 12,
    )
    fees = report.fee_report()
    assert fees["unpriced_fills"] == 12
    assert fees["priced_fills"] == 0
    assert any(
        item["hypothesis"] == "fees_paid_in_an_unpriced_asset"
        for item in report.hypotheses()
    )


def test_slippage_is_measured_against_the_decision_quote():
    report = CostReport(model=CostModel(), fills=[])
    bought = fill(price="100.05", reference="100", side="BUY")
    sold = fill(price="99.95", reference="100", side="SELL")

    assert CostReport.slippage_bps(bought) == Decimal("5.00")
    assert CostReport.slippage_bps(sold) == Decimal("5.00")


def test_a_fill_without_a_decision_quote_has_no_slippage_reading():
    assert CostReport.slippage_bps(fill(reference=None)) is None


def test_paper_fills_are_not_evidence_about_the_venue():
    """A paper fill's fee is the model's own arithmetic echoed back."""

    report = CostReport(model=CostModel(), fills=[fill(mode="paper")] * 20)

    assert report.fee_report()["fills"] == 0
    assert report.hypotheses()[0]["hypothesis"] == "not_enough_fills"


def test_a_small_sample_does_not_claim_the_model_is_wrong():
    report = CostReport(model=CostModel(), fills=[fill(fee_quote="0.3")] * 3)

    assert [item["hypothesis"] for item in report.hypotheses()] == ["not_enough_fills"]


def test_a_persistently_higher_fee_is_named():
    # 0.15 USDT on 100 USDT of notional is 15 bps against a 10 bps model.
    report = CostReport(model=CostModel(), fills=[fill(fee_quote="0.15")] * 12)

    names = [item["hypothesis"] for item in report.hypotheses()]
    assert "fees_higher_than_modelled" in names


def test_a_fee_that_matches_the_model_produces_no_hypothesis():
    report = CostReport(model=CostModel(), fills=[fill(fee_quote="0.1")] * 12)

    assert report.hypotheses() == []


def test_the_round_trip_report_adds_both_sides():
    report = CostReport(
        model=CostModel(),
        fills=[fill(fee_quote="0.1", price="100", reference="100")] * 12,
    )
    round_trip = report.round_trip_report()

    # 10 bps fee each way, no measured slippage against the reference.
    assert round_trip["realised_round_trip_bps"] == pytest.approx(20.0, abs=0.01)
    assert round_trip["modelled_round_trip_bps"] == 30.0
