# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19979 golden chain: history -> read effect -> estimate compute, kept out of measured sums.

AC1: estimated and measured figures are never summed together. The falsifier is
a fixture with one of each where Overview's measured total includes the
estimate; here Overview's measured total is the metering fold over the measured
run, and the estimate row is refused by that fold's types.

AC2: the CSV's baseline column equals Overview's baseline string.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.savings_estimate import (
    ESTIMATE_CSV_COLUMNS,
    EnumSavingsBasis,
    ModelEstimateDelegateRoute,
)
from omnimarket.nodes.node_claude_code_history_read_effect import (
    EnumHistoryReadStatus,
    HandlerClaudeCodeHistoryRead,
    ModelClaudeCodeHistoryReadRequest,
)
from omnimarket.nodes.node_metering_summary_compute import (
    ModelMeteringRecord,
    counterfactual_cost_usd,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import resolve_baseline
from omnimarket.nodes.node_savings_estimate_compute import (
    HandlerSavingsEstimate,
    ModelSavingsEstimateRequest,
    ModelSavingsEstimateResult,
)
from omnimarket.pricing import resolve_tier_cost
from omnimarket.projection.sqlite_metering_summary import (
    resolve_metering_baseline_model,
)
from tests.nodes.node_claude_code_history_read_effect.history_fixture import (
    recorded_source,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
_ESTIMATE_TOPIC = "onex.evt.omnimarket.savings-estimate-computed.v1"  # onex-topic-allow: this node's declared terminal
_AS_OF = datetime(2026, 10, 2, tzinfo=UTC)


def _overview_baseline_model() -> str:
    """The baseline string Overview's metering rows are folded and read under."""
    return resolve_metering_baseline_model().model


def _local_route() -> ModelEstimateDelegateRoute:
    cost = resolve_tier_cost("local")
    assert cost is not None, "routing_tiers.yaml must type the local tier's cost"
    return ModelEstimateDelegateRoute(tier_name="local", model="local", cost=cost)


def _estimate_from_recorded_history() -> ModelSavingsEstimateResult:
    read = HandlerClaudeCodeHistoryRead(
        source_factory=lambda _req: recorded_source()
    ).handle(ModelClaudeCodeHistoryReadRequest(opt_in=True, source_root="/recorded"))
    assert read.status is EnumHistoryReadStatus.READ
    baseline_model = _overview_baseline_model()
    return HandlerSavingsEstimate().handle(
        ModelSavingsEstimateRequest(
            records=read.records,
            baseline_model=baseline_model,
            baseline=resolve_baseline(baseline_model),
            route=_local_route(),
        )
    )


def _measured_run() -> ModelMeteringRecord:
    return ModelMeteringRecord(
        correlation_id="measured-1",
        occurred_at=datetime(2026, 10, 1, 9, tzinfo=UTC),
        model="local",
        task_type="code_generation",
        tokens_in=1000,
        tokens_out=100,
        spend_usd=Decimal("0"),
    )


def _overview_fold(
    *records: ModelMeteringRecord,
) -> tuple[ModelMeteringSummaryRow, ...]:
    baseline_model = _overview_baseline_model()
    return (
        HandlerProjectionMeteringSummary()
        .handle(
            ModelMeteringSummaryFoldRequest(
                tenant_id="local",
                records=records,
                baseline=resolve_baseline(baseline_model),
                baseline_model=baseline_model,
                as_of=_AS_OF,
            )
        )
        .rows
    )


def test_history_to_estimate_rows_golden_chain() -> None:
    result = _estimate_from_recorded_history()
    assert result.basis is EnumSavingsBasis.ESTIMATED
    assert (
        result.prompts_total,
        result.prompts_estimated,
        result.prompts_without_usage,
    ) == (2, 1, 1)
    row = result.rows[0]
    assert (row.prompt_id, row.source_model, row.tokens_in, row.tokens_out) == (
        "u1",
        "claude-opus-4-6",
        1300,
        300,
    )
    baseline = resolve_baseline(_overview_baseline_model())
    assert baseline is not None, "Overview's default baseline must be priced"
    assert row.estimated_baseline_cost_usd == counterfactual_cost_usd(
        baseline=baseline, tokens_in=1300, tokens_out=300
    )
    assert row.estimated_delegate_cost_usd == Decimal("0")
    assert row.pricing_manifest_version == baseline.pricing_manifest_version


def test_overview_measured_total_excludes_the_estimate() -> None:
    estimate = _estimate_from_recorded_history()
    assert estimate.estimated_savings_usd is not None
    assert estimate.estimated_savings_usd > 0
    measured = _measured_run()

    overview = {row.window_kind: row for row in _overview_fold(measured)}["all"]
    baseline = resolve_baseline(_overview_baseline_model())
    assert baseline is not None
    measured_saving = counterfactual_cost_usd(
        baseline=baseline, tokens_in=1000, tokens_out=100
    ) - Decimal("0")
    assert overview.runs_total == overview.runs_measured == 1
    assert Decimal(overview.savings_usd or "nan") == measured_saving
    assert Decimal(overview.savings_usd or "nan") != (
        measured_saving + estimate.estimated_savings_usd
    )

    # The estimate cannot be smuggled into the measured fold, as a row or as data.
    with pytest.raises(ValidationError):
        ModelMeteringSummaryFoldRequest(
            tenant_id="local",
            records=(measured, estimate.rows[0]),
            baseline=baseline,
            baseline_model=baseline.model,
            as_of=_AS_OF,
        )
    with pytest.raises(ValidationError):
        ModelMeteringRecord.model_validate(estimate.rows[0].model_dump())


def test_csv_baseline_column_equals_overview_baseline() -> None:
    estimate = _estimate_from_recorded_history()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ESTIMATE_CSV_COLUMNS)
    writer.writerows(row.csv_row() for row in estimate.rows)
    parsed = list(csv.DictReader(io.StringIO(buffer.getvalue())))
    assert parsed, "the recorded history must yield at least one CSV row"

    overview = {row.window_kind: row for row in _overview_fold(_measured_run())}["all"]
    assert {line["baseline_model"] for line in parsed} == {overview.baseline_model}
    assert {line["basis"] for line in parsed} == {"estimated"}


def test_no_measured_node_subscribes_to_the_estimate_topic() -> None:
    subscribers = []
    for contract_path in sorted(_NODES.glob("*/contract.yaml")):
        contract = yaml.safe_load(contract_path.read_text()) or {}
        topics = (contract.get("event_bus") or {}).get("subscribe_topics") or []
        if _ESTIMATE_TOPIC in topics:
            subscribers.append(contract_path.parent.name)
    assert subscribers == []
    own = yaml.safe_load(
        (_NODES / "node_savings_estimate_compute/contract.yaml").read_text()
    )
    assert own["terminal_event"] == _ESTIMATE_TOPIC
    assert _ESTIMATE_TOPIC in own["event_bus"]["publish_topics"]
    assert _ESTIMATE_TOPIC in own["externally_consumed_topics"]
