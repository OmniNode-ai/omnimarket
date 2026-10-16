# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC3: individual modelled savings from the one metering compute.

Realistic failures: serving the window total/average on every run; reusing the
legacy unbaselined savings column; turning an unmeasured run into zero; hiding a
measured zero or clamping a negative saving; losing the named baseline; and
individual figures disagreeing with the headline computed from the same records.
Written before adding per-run output to the compute.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omnimarket.nodes.node_metering_summary_compute import (
    EnumMeteringMeasurement,
    HandlerMeteringSummary,
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
    ModelMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)

NOW = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
BASELINE = ModelCounterfactualBaseline(
    model="fixture-baseline",
    price_in_per_1k=Decimal("0.010"),
    price_out_per_1k=Decimal("0.020"),
    as_of="2026-10-13",
    pricing_manifest_version="fixture-v1",
    source="test_fixture",
)


def record(correlation_id: str, **changes: object) -> ModelMeteringRecord:
    fields: dict[str, object] = {
        "correlation_id": correlation_id,
        "occurred_at": datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        "model": "fixture-serving-model",
        "tokens_in": 1000,
        "tokens_out": 200,
        "spend_usd": Decimal("0.004"),
    }
    fields.update(changes)
    return ModelMeteringRecord.model_validate(fields)


def fold(
    *records: ModelMeteringRecord,
    baseline: ModelCounterfactualBaseline | None = BASELINE,
) -> ModelMeteringSummary:
    return HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=ModelMeteringWindow(label="all", end=NOW),
            records=records,
            baseline=baseline,
        )
    )


def test_each_run_has_its_own_saving_and_they_reconcile_with_the_headline() -> None:
    result = fold(
        record("first"),
        record("second", tokens_in=100, tokens_out=100, spend_usd=Decimal("0.005")),
    )
    assert [(r.correlation_id, r.savings_usd) for r in result.by_run] == [
        ("first", Decimal("0.010")),
        ("second", Decimal("-0.002")),
    ]
    assert (
        sum(r.savings_usd for r in result.by_run if r.savings_usd is not None)
        == result.savings_usd
    )


def test_legacy_recorded_savings_is_not_relabelled_as_modelled_savings() -> None:
    result = fold(record("legacy", recorded_savings_usd=Decimal("999")))
    assert result.by_run[0].savings_usd == Decimal("0.010")
    assert result.reconciliation.recorded_savings_usd == Decimal("999")


@pytest.mark.parametrize(
    ("changes", "measurement"),
    [
        (
            {"tokens_in": None, "tokens_out": None},
            EnumMeteringMeasurement.UNKNOWN_TOKENS,
        ),
        ({"spend_usd": None}, EnumMeteringMeasurement.UNKNOWN_SPEND),
    ],
)
def test_an_unmeasured_run_is_null_and_keeps_its_measurement_class(
    changes: dict[str, object], measurement: EnumMeteringMeasurement
) -> None:
    result = fold(record("unknown", **changes))
    assert result.by_run[0].correlation_id == "unknown"
    assert result.by_run[0].savings_usd is None
    assert result.by_run[0].measurement == measurement


def test_a_measured_zero_is_zero_and_distinct_from_an_unmeasured_run() -> None:
    result = fold(
        record("zero", spend_usd=Decimal("0.014")),
        record("unknown", spend_usd=None),
    )
    assert result.by_run[0].savings_usd == Decimal("0")
    assert result.by_run[0].measurement == EnumMeteringMeasurement.MEASURED
    assert result.by_run[1].savings_usd is None


def test_an_unresolved_baseline_has_no_per_run_figure_or_baseline_name() -> None:
    result = fold(record("unresolved"), baseline=None)
    assert result.by_run[0].savings_usd is None
    assert result.by_run[0].baseline_model is None
    assert result.by_run[0].pricing_manifest_version is None


def test_each_figure_carries_the_same_named_baseline_and_manifest_as_the_headline() -> (
    None
):
    result = fold(record("priced"), record("unpriced", spend_usd=None))
    for row in result.by_run:
        assert row.baseline_model == result.baseline.model
        assert row.pricing_manifest_version == result.baseline.pricing_manifest_version


@pytest.mark.parametrize(
    "change",
    [
        {"baseline_model": None},
        {"pricing_manifest_version": None},
        {"measurement": EnumMeteringMeasurement.UNKNOWN_SPEND},
    ],
)
def test_a_per_run_figure_cannot_lose_its_basis_or_measurement(
    change: dict[str, object],
) -> None:
    payload = fold(record("priced")).model_dump()
    payload["by_run"][0].update(change)
    with pytest.raises(ValueError, match="requires measured usage"):
        ModelMeteringSummary.model_validate(payload)


def test_aggregate_projection_does_not_embed_each_runs_details_in_every_window() -> (
    None
):
    from omnimarket.nodes.node_projection_metering_summary import (
        HandlerProjectionMeteringSummary,
        ModelMeteringSummaryFoldRequest,
    )

    result = HandlerProjectionMeteringSummary().handle(
        ModelMeteringSummaryFoldRequest(
            tenant_id="6c24401e-4fdd-4788-a6d3-161b579e0fe0",
            baseline_model=BASELINE.model,
            baseline=BASELINE,
            records=(record("first"), record("second")),
            as_of=NOW,
        )
    )
    for row in result.rows:
        assert "by_run" not in json.loads(row.summary_json)
