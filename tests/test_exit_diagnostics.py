"""Exit diagnostics: turn a recorded path into a hypothesis, not a tweak."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jev_trading.services.exit_diagnostics import ExitDiagnostics, format_report


@dataclass
class Row:
    exit_reason: str
    net_bps: Decimal | None
    mfe_bps: Decimal | None
    mae_bps: Decimal | None
    round_trip_cost_bps: Decimal | None = Decimal("30")
    holding_seconds: int | None = 3600
    time_to_mfe_seconds: int | None = 600


def test_a_timeout_that_never_covered_cost_is_not_an_exit_problem():
    """The market never offered the trade; no exit rule could have saved it."""

    rows = [
        Row("timeout", Decimal("-35"), Decimal("10"), Decimal("-50")),
        Row("timeout", Decimal("-40"), Decimal("20"), Decimal("-60")),
    ]

    report = ExitDiagnostics(rows=rows).as_dict()

    assert report["timeout"]["never_covered_cost"] == 2
    assert report["timeout"]["never_covered_cost_rate"] == 1.0
    assert report["timeout"]["peaked_above_giveback_floor"] == 0
    names = [item["hypothesis"] for item in report["hypotheses"]]
    assert "market_never_offered_the_trade" in names
    assert "profit_was_available_and_given_back" not in names


def test_a_timeout_that_peaked_high_is_a_giveback_problem():
    """Price paid; the exit did not keep it. That is an exit-geometry question."""

    rows = [
        Row("timeout", Decimal("-20"), Decimal("55"), Decimal("-10")),
        Row("timeout", Decimal("-25"), Decimal("70"), Decimal("-15")),
    ]

    report = ExitDiagnostics(rows=rows).as_dict()

    assert report["timeout"]["peaked_above_giveback_floor"] == 2
    assert report["timeout"]["never_covered_cost"] == 0
    names = [item["hypothesis"] for item in report["hypotheses"]]
    assert "profit_was_available_and_given_back" in names
    assert "market_never_offered_the_trade" not in names


def test_capture_ratio_measures_what_the_exit_kept():
    """A row that realised most of its MFE has a ratio near 1; a giveback near 0."""

    kept = Row("take_profit", Decimal("90"), Decimal("100"), Decimal("-5"))
    given_back = Row("timeout", Decimal("-30"), Decimal("80"), Decimal("-20"))

    report = ExitDiagnostics(rows=[kept, given_back]).as_dict()

    # gross = net + cost: 120 of 100 and 0 of 80 -> median ratio 0.6.
    assert report["capture"]["rows_with_positive_mfe"] == 2
    assert report["capture"]["median_capture_ratio"] == 0.6


def test_cost_eating_a_positive_gross_is_named_as_a_cost_problem():
    rows = [Row("take_profit", Decimal("-5"), Decimal("60"), Decimal("-2"))]

    report = ExitDiagnostics(rows=rows).as_dict()

    assert report["cost"]["average_gross_bps"] == 25.0
    assert report["cost"]["average_net_bps"] == -5.0
    names = [item["hypothesis"] for item in report["hypotheses"]]
    assert "cost_eats_the_whole_edge" in names


def test_undecided_rows_are_not_counted_as_outcomes():
    """no_data rows carry no verdict; they must not move an average."""

    rows = [
        Row("no_data", None, None, None, round_trip_cost_bps=None, holding_seconds=None),
        Row("timeout", Decimal("-35"), Decimal("10"), Decimal("-50")),
    ]

    report = ExitDiagnostics(rows=rows).as_dict()

    assert report["rows"] == 2
    assert report["decided"] == 1
    assert report["by_reason"] == {"no_data": 1, "timeout": 1}
    assert report["cost"]["average_net_bps"] == -35.0


def test_no_rows_produces_no_hypotheses_and_no_crash():
    report = ExitDiagnostics(rows=[]).as_dict()

    assert report["rows"] == 0
    assert report["hypotheses"] == []
    assert report["timeout"] == {"timeouts": 0}
    assert "No hypothesis" in format_report(report)
