# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20007: the daily series reads metering-summary.v1 day rows.

Under the 2026-10-04 ruling the pages' daily savings series is the ``day`` rows
of ``node_projection_metering_summary``, so those rows must add up to the
headline ``all`` row for the same snapshot (AC3), and folding the same records
again must give the same rows (AC4).

Failure modes these tests are written against:

* a run that sits on either side of a UTC midnight lands in the wrong day, or
  in two days, or in none;
* an idle day between two run days reads as zero money instead of no money;
* an unmeasured run (no tokens, or tokens but no spend) leaks into a money sum,
  or drops out of ``runs_total``;
* money summed as floats drifts from the exact Decimal total;
* a run at exactly ``as_of`` is counted;
* an unresolved baseline turns savings into a number on some rows;
* the order the records arrive in changes a row.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    EnumBaselineState,
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)

pytestmark = pytest.mark.unit

AS_OF = datetime(2026, 9, 28, 12, tzinfo=UTC)
IDLE_DAY = date(2026, 9, 26)
BASELINE = ModelCounterfactualBaseline(
    model="baseline-model",
    price_in_per_1k=Decimal("3"),
    price_out_per_1k=Decimal("15"),
    as_of="2026-09-01",
    pricing_manifest_version="1",
    source="pricing_manifest",
)


def _run(
    cid: str,
    stamp: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    spend: str | None,
) -> ModelMeteringRecord:
    return ModelMeteringRecord(
        correlation_id=cid,
        occurred_at=datetime.fromisoformat(stamp),
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        spend_usd=None if spend is None else Decimal(spend),
    )


RECORDS = (
    _run("d25-a", "2026-09-25T09:00:00+00:00", "local-a", 1000, 200, "0.1"),
    _run("d25-b", "2026-09-25T23:59:59.999999+00:00", "local-b", 400, 100, "0.2"),
    # 2026-09-26 is idle.
    _run("d27-a", "2026-09-27T00:00:00+00:00", "local-a", 2000, 300, "0.3"),
    # 2026-09-27T20:00-04:00 is 2026-09-28T00:00Z: it belongs to the 28th.
    _run("d28-tz", "2026-09-27T20:00:00-04:00", "local-b", 100, 50, "0.7"),
    _run("d27-notok", "2026-09-27T08:00:00+00:00", "local-a", None, None, None),
    _run("d27-nospend", "2026-09-27T09:00:00+00:00", "local-b", 500, 50, None),
    _run("d28-a", "2026-09-28T11:59:59.999999+00:00", "local-a", 300, 30, "0.05"),
    # Exactly at as_of: excluded from every window.
    _run("at-as-of", AS_OF.isoformat(), "local-a", 9000, 9000, "9"),
)

# Independent oracle, by hand from the records above (measured runs only:
# d25-a, d25-b, d27-a, d28-tz, d28-a). Counterfactual = 3/1k in + 15/1k out.
# Each value is (runs_total, spend_usd, counterfactual_usd).
EXPECTED_DAYS: dict[str, tuple[int, str | None, str | None]] = {
    "2026-09-25": (2, "0.3", "8.7"),
    "2026-09-26": (0, None, None),
    "2026-09-27": (3, "0.3", "10.5"),
    "2026-09-28": (2, "0.75", "2.4"),
}
EXPECTED_ALL: tuple[int, str, str] = (7, "1.35", "21.6")


def _fold(
    records: tuple[ModelMeteringRecord, ...] = RECORDS,
    baseline: ModelCounterfactualBaseline | None = BASELINE,
) -> tuple[ModelMeteringSummaryRow, ...]:
    """The production shape: recorded days plus an explicitly requested idle day."""
    days = frozenset({IDLE_DAY}) | {
        r.occurred_at.astimezone(UTC).date() for r in records
    }
    return (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id="tenant-20007",
                records=records,
                baseline=baseline,
                baseline_model="baseline-model",
                as_of=AS_OF,
                days=days,
            )
        )
        .rows
    )


def _split(
    rows: tuple[ModelMeteringSummaryRow, ...],
) -> tuple[ModelMeteringSummaryRow, list[ModelMeteringSummaryRow]]:
    (all_row,) = [r for r in rows if r.window_kind == "all"]
    return all_row, [r for r in rows if r.window_kind == "day"]


def _money_sum(values: list[str | None]) -> Decimal | None:
    """Sum the known values exactly; no known value means no figure, not zero."""
    known = [Decimal(v) for v in values if v is not None]
    return sum(known, Decimal("0")) if known else None


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def test_each_day_row_matches_the_hand_computed_day() -> None:
    _, days = _split(_fold())
    assert {d.window_start for d in days} == set(EXPECTED_DAYS)
    for day in days:
        runs, spend_text, cf_text = EXPECTED_DAYS[day.window_start]
        spend, cf = _dec(spend_text), _dec(cf_text)
        assert day.runs_total == runs, day.window_start
        assert _dec(day.spend_usd) == spend, day.window_start
        assert _dec(day.counterfactual_usd) == cf, day.window_start
        savings = None if spend is None or cf is None else cf - spend
        assert _dec(day.savings_usd) == savings, day.window_start


def test_day_rows_sum_exactly_to_the_all_row() -> None:
    all_row, days = _split(_fold())
    runs, spend, cf = EXPECTED_ALL
    assert all_row.runs_total == runs
    assert _dec(all_row.spend_usd) == Decimal(spend)
    assert _dec(all_row.counterfactual_usd) == Decimal(cf)
    for field in ("spend_usd", "counterfactual_usd", "savings_usd"):
        assert _money_sum([getattr(d, field) for d in days]) == _dec(
            getattr(all_row, field)
        ), field
    for field in (
        "runs_total",
        "runs_measured",
        "runs_unknown_tokens",
        "runs_unknown_spend",
        "tokens_in",
        "tokens_out",
    ):
        assert sum(getattr(d, field) for d in days) == getattr(all_row, field), field


def test_unmeasured_runs_count_but_carry_no_money() -> None:
    all_row, days = _split(_fold())
    assert (all_row.runs_measured, all_row.runs_unknown_tokens) == (5, 1)
    assert all_row.runs_unknown_spend == 1
    (idle,) = [d for d in days if d.window_start == IDLE_DAY.isoformat()]
    assert idle.runs_total == 0
    assert (idle.spend_usd, idle.counterfactual_usd, idle.savings_usd) == (
        None,
        None,
        None,
    )


def test_unresolved_baseline_keeps_spend_sums_and_no_savings_anywhere() -> None:
    all_row, days = _split(_fold(baseline=None))
    for row in (all_row, *days):
        assert row.baseline_state == EnumBaselineState.UNRESOLVED
        assert row.savings_usd is None
        assert row.counterfactual_usd is None
    assert _money_sum([d.spend_usd for d in days]) == _dec(all_row.spend_usd)
    assert _dec(all_row.spend_usd) == Decimal(EXPECTED_ALL[1])


def test_without_requested_days_every_record_lands_in_its_utc_day() -> None:
    """``days=None`` is the bus writer's default: the fold picks the days itself.

    The second run is 2026-09-26T01:00Z written with a -04:00 offset, so its
    local calendar date is the 25th; it is the only run on its UTC day.
    """
    records = (
        RECORDS[0],
        _run("d26-tz", "2026-09-25T21:00:00-04:00", "local-b", 100, 10, "0.4"),
    )
    rows = (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id="tenant-20007",
                records=records,
                baseline=BASELINE,
                baseline_model="baseline-model",
                as_of=AS_OF,
            )
        )
        .rows
    )
    all_row, days = _split(rows)
    assert [(d.window_start, d.runs_total) for d in days] == [
        ("2026-09-25", 1),
        ("2026-09-26", 1),
    ]
    for field in ("spend_usd", "counterfactual_usd", "savings_usd"):
        assert _money_sum([getattr(d, field) for d in days]) == _dec(
            getattr(all_row, field)
        ), field
    assert _dec(all_row.spend_usd) == Decimal("0.5")


def test_a_replayed_fold_gives_the_same_rows_in_any_record_order() -> None:
    first = _fold()
    assert _fold() == first
    assert _fold(tuple(reversed(RECORDS))) == first
    assert _fold(RECORDS[3:] + RECORDS[:3]) == first
