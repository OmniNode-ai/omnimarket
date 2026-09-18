# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Metering summary arithmetic and unknown-classification rules (OMN-18697).

The figures below are exact and hand-checkable from the fixture baseline, so a
regression in the arithmetic fails on a specific wrong number rather than on an
approximate comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    EnumMeteringMeasurement,
    HandlerMeteringSummary,
    ModelCounterfactualBaseline,
    ModelMeteringReconciliation,
    ModelMeteringRecord,
    ModelMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The real manifest's Claude Opus prices, pinned here so the expected figures
#: below can be recomputed by hand: $0.015/1k in, $0.075/1k out.
BASELINE = ModelCounterfactualBaseline(
    model="claude-opus-4-6",
    price_in_per_1k=Decimal("0.015"),
    price_out_per_1k=Decimal("0.075"),
    as_of="2026-02-01",
    pricing_manifest_version="1.0.0",
    source="pricing_manifest",
)


def _record(
    correlation_id: str,
    *,
    model: str = "Qwen3.8-27B",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    spend_usd: Decimal | None = None,
    recorded_savings_usd: Decimal | None = None,
) -> ModelMeteringRecord:
    return ModelMeteringRecord(
        correlation_id=correlation_id,
        occurred_at=_NOW,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        spend_usd=spend_usd,
        recorded_savings_usd=recorded_savings_usd,
    )


#: Two fully measured runs, one with no tokens, one with tokens but no spend.
FIXTURE_RECORDS = (
    _record("r1", tokens_in=1000, tokens_out=1000, spend_usd=Decimal("0")),
    _record("r2", tokens_in=2000, tokens_out=500, spend_usd=Decimal("0.002")),
    _record("r3", recorded_savings_usd=Decimal("0.0")),
    _record("r4", tokens_in=100, tokens_out=100),
)


def _summarize(
    records: tuple[ModelMeteringRecord, ...] = FIXTURE_RECORDS,
    *,
    baseline: ModelCounterfactualBaseline | None = BASELINE,
) -> ModelMeteringSummary:
    return HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=ModelMeteringWindow(label="7d", start=None, end=_NOW),
            records=records,
            baseline=baseline,
        )
    )


class TestExactFigures:
    """Hand-checkable arithmetic over the fixture."""

    def test_counterfactual_and_savings_are_exact(self) -> None:
        summary = _summarize()
        # r1: 0.015*1 + 0.075*1                 = 0.0900
        # r2: 0.015*2 + 0.075*0.5               = 0.0675
        assert summary.counterfactual_usd == Decimal("0.1575")
        assert summary.spend_usd == Decimal("0.002")
        assert summary.savings_usd == Decimal("0.1555")

    def test_tokens_include_runs_whose_spend_is_unknown(self) -> None:
        """ "How much work went through" is answerable where "what it saved" is not."""
        summary = _summarize()
        assert summary.tokens_in == 3100
        assert summary.tokens_out == 1600

    def test_savings_is_exactly_counterfactual_minus_spend(self) -> None:
        summary = _summarize()
        assert summary.counterfactual_usd is not None
        assert summary.spend_usd is not None
        assert summary.savings_usd == summary.counterfactual_usd - summary.spend_usd

    def test_arithmetic_is_decimal_not_float(self) -> None:
        """A float here accumulates error across tens of thousands of rows."""
        summary = _summarize()
        assert isinstance(summary.savings_usd, Decimal)


class TestUnknownIsNeverZero:
    """AC3: a run that produced no measurement is UNKNOWN, not a zero."""

    def test_every_run_lands_in_exactly_one_class(self) -> None:
        summary = _summarize()
        assert summary.runs_total == 4
        assert summary.runs_measured == 2
        assert summary.runs_unknown_tokens == 1
        assert summary.runs_unknown_spend == 1

    def test_a_run_with_no_tokens_is_unknown_tokens(self) -> None:
        assert _record("x").measurement is EnumMeteringMeasurement.UNKNOWN_TOKENS

    def test_a_run_with_tokens_and_no_spend_is_unknown_spend(self) -> None:
        record = _record("x", tokens_in=10, tokens_out=10)
        assert record.measurement is EnumMeteringMeasurement.UNKNOWN_SPEND

    def test_a_genuinely_free_run_is_measured_not_unknown(self) -> None:
        """Zero spend is a measurement when the record says zero."""
        record = _record("x", tokens_in=10, tokens_out=10, spend_usd=Decimal("0"))
        assert record.measurement is EnumMeteringMeasurement.MEASURED

    def test_an_unmeasured_window_reports_unknown_not_zero(self) -> None:
        """The falsifier: a zero that cannot be told from an unmeasured run."""
        summary = _summarize((_record("only"),))
        assert summary.runs_total == 1
        assert summary.runs_unknown_tokens == 1
        assert summary.savings_usd is None
        assert summary.spend_usd is None
        assert summary.counterfactual_usd is None

    def test_unknown_runs_do_not_dilute_the_measured_totals(self) -> None:
        """Adding unmeasured runs changes the counts, never the money."""
        with_unknowns = _summarize()
        measured_only = _summarize(FIXTURE_RECORDS[:2])
        assert with_unknowns.savings_usd == measured_only.savings_usd
        assert with_unknowns.runs_total != measured_only.runs_total


class TestBaselineTravelsWithTheFigure:
    """AC2: a savings figure always carries the baseline it was derived against."""

    def test_the_baseline_is_echoed_on_the_summary(self) -> None:
        summary = _summarize()
        assert summary.baseline_state is EnumBaselineState.RESOLVED
        assert summary.baseline == BASELINE

    def test_an_unresolved_baseline_yields_no_figure_rather_than_zero(self) -> None:
        summary = _summarize(baseline=None)
        assert summary.baseline_state is EnumBaselineState.UNRESOLVED
        assert summary.baseline is None
        assert summary.savings_usd is None
        assert summary.counterfactual_usd is None
        # Spend is still a measurement and is still reported.
        assert summary.spend_usd == Decimal("0.002")
        assert summary.runs_measured == 2

    def test_the_headline_is_recomputable_from_the_printed_baseline(self) -> None:
        """A reader with only the output can check the number."""
        summary = _summarize()
        assert summary.baseline is not None
        measured = FIXTURE_RECORDS[:2]
        by_hand = sum(
            (
                summary.baseline.price_in_per_1k * Decimal(r.tokens_in or 0)
                + summary.baseline.price_out_per_1k * Decimal(r.tokens_out or 0)
            )
            / Decimal("1000")
            for r in measured
        )
        assert summary.counterfactual_usd == by_hand

    def test_a_summary_cannot_be_built_with_a_figure_and_no_baseline(self) -> None:
        """The model refuses the falsifier, so no future code path can emit it."""
        with pytest.raises(ValidationError, match="must carry the counterfactual"):
            ModelMeteringSummary(
                window=ModelMeteringWindow(label="7d", end=_NOW),
                runs_total=0,
                runs_measured=0,
                runs_unknown_tokens=0,
                runs_unknown_spend=0,
                tokens_in=0,
                tokens_out=0,
                savings_usd=Decimal("1.00"),
                baseline_state=EnumBaselineState.UNRESOLVED,
                baseline=None,
                reconciliation=ModelMeteringReconciliation(
                    recorded_savings_usd=Decimal("0"), recorded_savings_runs=0
                ),
            )

    def test_a_summary_whose_classes_do_not_sum_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="exactly one measurement class"):
            ModelMeteringSummary(
                window=ModelMeteringWindow(label="7d", end=_NOW),
                runs_total=5,
                runs_measured=1,
                runs_unknown_tokens=1,
                runs_unknown_spend=1,
                tokens_in=0,
                tokens_out=0,
                baseline_state=EnumBaselineState.UNRESOLVED,
                reconciliation=ModelMeteringReconciliation(
                    recorded_savings_usd=Decimal("0"), recorded_savings_runs=0
                ),
            )


class TestRecordedColumnIsQuarantined:
    """The writer's own savings column is reconciliation, not the headline."""

    def test_it_is_reported_separately_and_flagged_baseline_less(self) -> None:
        records = (
            _record("a", tokens_in=1, tokens_out=1, recorded_savings_usd=Decimal("9")),
            _record("b", recorded_savings_usd=Decimal("1")),
        )
        summary = _summarize(records)
        assert summary.reconciliation.recorded_savings_usd == Decimal("10")
        assert summary.reconciliation.recorded_savings_runs == 2
        assert summary.reconciliation.recorded_savings_baseline_available is False

    def test_it_counts_unknown_runs_that_the_headline_excludes(self) -> None:
        """This is precisely why summing that column overstates the window."""
        summary = _summarize()
        assert summary.reconciliation.recorded_savings_runs == 1
        assert summary.runs_measured == 2


class TestPerModelBreakdown:
    def test_rows_are_ordered_by_runs_then_name(self) -> None:
        records = (
            _record("a", model="zeta", tokens_in=1, tokens_out=1),
            _record("b", model="alpha", tokens_in=1, tokens_out=1),
            _record("c", model="alpha", tokens_in=1, tokens_out=1),
        )
        summary = _summarize(records)
        assert [row.model for row in summary.by_model] == ["alpha", "zeta"]

    def test_a_model_with_no_measured_run_reports_unknown_not_zero(self) -> None:
        summary = _summarize((_record("a", model="alpha", tokens_in=1, tokens_out=1),))
        assert summary.by_model[0].spend_usd is None
        assert summary.by_model[0].savings_usd is None

    def test_an_unrecorded_model_is_named_rather_than_dropped(self) -> None:
        summary = _summarize((_record("a", model=""),))
        assert summary.by_model[0].model == "(unrecorded)"

    def test_top_models_bounds_the_breakdown(self) -> None:
        records = tuple(
            _record(f"r{i}", model=f"m{i}", tokens_in=1, tokens_out=1) for i in range(5)
        )
        summary = HandlerMeteringSummary().handle(
            ModelMeteringSummaryRequest(
                window=ModelMeteringWindow(label="7d", end=_NOW),
                records=records,
                baseline=BASELINE,
                top_models=2,
            )
        )
        assert len(summary.by_model) == 2
        assert summary.runs_total == 5


class TestDeterminism:
    def test_the_same_input_yields_the_same_output(self) -> None:
        assert _summarize() == _summarize()

    def test_an_empty_window_is_all_zeroes_and_no_figure(self) -> None:
        summary = _summarize(())
        assert summary.runs_total == 0
        assert summary.savings_usd is None
        assert summary.reconciliation.recorded_savings_usd == Decimal("0")


class TestContractShape:
    """The wire contract, pinned. Changing a topic is then a diff on this test."""

    @staticmethod
    def _contract() -> dict[str, object]:
        path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_metering_summary_compute"
            / "contract.yaml"
        )
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def test_the_terminal_event_is_the_declared_one(self) -> None:
        assert (
            self._contract()["terminal_event"]
            == "onex.evt.omnimarket.metering-summary-computed.v1"
        )

    def test_the_terminal_event_is_published_and_declared_a_sink(self) -> None:
        contract = self._contract()
        event_bus = contract["event_bus"]
        assert isinstance(event_bus, dict)
        assert event_bus["publish_topics"] == [
            "onex.evt.omnimarket.metering-summary-computed.v1"
        ]
        assert contract["externally_consumed_topics"] == [
            "onex.evt.omnimarket.metering-summary-computed.v1"
        ]

    def test_the_command_topic_is_the_subscribed_one(self) -> None:
        contract = self._contract()
        event_bus = contract["event_bus"]
        assert isinstance(event_bus, dict)
        runtime_dispatch = contract["runtime_dispatch"]
        assert isinstance(runtime_dispatch, dict)
        assert event_bus["subscribe_topics"] == [
            "onex.cmd.omnimarket.metering-summary-requested.v1"
        ]
        assert (
            runtime_dispatch["command_topic"]
            == "onex.cmd.omnimarket.metering-summary-requested.v1"
        )

    def test_the_node_declares_itself_pure(self) -> None:
        """A handler that read the database would make every figure untestable."""
        descriptor = self._contract()["descriptor"]
        assert isinstance(descriptor, dict)
        assert descriptor["node_archetype"] == "compute"
        assert descriptor["purity"] == "pure"
        assert descriptor["idempotent"] is True
