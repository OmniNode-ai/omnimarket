# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC1: a run that ended with a failure cause saves nothing.

Read on a copy of an operator store on 2026-11-16: 407 runs ended
``quality_gate_refused`` and were credited $4.9043 of modelled savings, because
only ``returned_to_caller`` was treated as saving nothing. A refused answer was
not used, so it saved nothing; its spend was real and still counts.

Failure modes, each with a test below:
  F1  the window's savings credit a run whose answer was refused
  F2  the refused run's spend disappears from the window's spend
  F3  the excluded run is not counted by its reason
  F4  the per-run modelled saving the savings view sums credits the refused run
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from omnimarket.nodes.node_metering_summary_compute import (
    HandlerMeteringSummary,
    ModelCounterfactualBaseline,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
    metering_record_from_row,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_run_savings import (
    modelled_run_savings_columns,
)

pytestmark = pytest.mark.unit

_BASELINE = ModelCounterfactualBaseline(
    model="claude-sonnet-5-5",
    pricing_manifest_version="test-manifest",
    price_in_per_1k=Decimal("0.003"),
    price_out_per_1k=Decimal("0.015"),
    as_of="2026-10-01",
    source="pricing_manifest",
)


def _row(cause: str | None) -> dict[str, object]:
    return {
        "correlation_id": str(uuid.uuid4()),
        "created_at": "2026-10-22T12:00:00+00:00",
        "delegated_to": "openai",
        "model_name": "gpt-4o-mini",
        "task_type": "code_generation",
        "tokens_input": 180,
        "tokens_output": 60,
        "cost_usd": 0.0006,
        "cost_savings_usd": 0.0,
        "terminal_failure_cause": cause,
    }


def _saving_of_one_measured_run() -> Decimal:
    counterfactual = _BASELINE.price_in_per_1k * Decimal(180) / Decimal(1000) + (
        _BASELINE.price_out_per_1k * Decimal(60) / Decimal(1000)
    )
    return counterfactual - Decimal("0.0006")


def test_a_refused_run_saves_nothing_keeps_its_spend_and_is_counted() -> None:
    """F1, F2, F3."""
    records = tuple(
        record
        for record in (
            metering_record_from_row(_row(None)),
            metering_record_from_row(_row("quality_gate_refused")),
        )
        if record is not None
    )
    assert len(records) == 2
    summary = HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=ModelMeteringWindow(
                label="all", end=records[0].occurred_at.replace(year=2027)
            ),
            records=records,
            baseline=_BASELINE,
        )
    )
    assert summary.savings_usd == _saving_of_one_measured_run()
    assert summary.spend_usd == Decimal("0.0012")
    assert summary.runs_failed == 1


def test_the_per_run_modelled_saving_of_a_refused_run_is_zero() -> None:
    """F4."""
    refused = modelled_run_savings_columns(_row("quality_gate_refused"))
    succeeded = modelled_run_savings_columns(_row(None))
    # The per-run column prices against the configured baseline, not _BASELINE: the
    # same tokens and spend earn a positive saving when the run succeeded, and none
    # when its answer was refused.
    assert Decimal(str(succeeded["modelled_savings_usd"])) > Decimal("0")
    assert Decimal(str(refused["modelled_savings_usd"])) == Decimal("0")
