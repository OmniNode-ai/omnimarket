# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation-owned adapter to the canonical metering compute's per-run result.

Called after terminal precedence and sparse-evidence preservation, so a saving
always describes the inputs actually written. No prices or money arithmetic
are defined at this boundary. The baseline is the same selection used by the
local metering refresh; its identity accompanies every persisted figure.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from omnimarket.models.priced_spend import priced_spend
from omnimarket.nodes.node_metering_summary_compute import (
    HandlerMeteringSummary,
    ModelMeteringRecord,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)
from omnimarket.nodes.node_projection_metering_summary import (
    baseline as baseline_pricing,
)
from omnimarket.projection import sqlite_metering_summary
from omnimarket.projection.sqlite_metering_reader import normalise_occurred_at

RUN_SAVINGS_INPUT_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "created_at",
    "timestamp",
    "task_type",
    "delegated_to",
    "model_name",
    "tokens_input",
    "tokens_output",
    "cost_usd",
    "terminal_failure_cause",
)
RUN_SAVINGS_DERIVED_COLUMNS: Final[tuple[str, ...]] = (
    "modelled_savings_usd",
    "savings_baseline_model",
    "savings_pricing_manifest_version",
    "savings_measurement",
)


def modelled_run_savings_columns(row: Mapping[str, object]) -> dict[str, object]:
    """The nullable figure and its basis, never the legacy recorded saving."""
    empty: dict[str, object] = {
        "modelled_savings_usd": None,
        "savings_baseline_model": None,
        "savings_pricing_manifest_version": None,
        "savings_measurement": "unknown_timestamp",
    }
    stamp = row.get("created_at") or row.get("timestamp")
    occurred_at = stamp if isinstance(stamp, datetime) else normalise_occurred_at(stamp)
    if occurred_at is None:
        return empty
    tokens_in = int(str(row.get("tokens_input") or 0))
    tokens_out = int(str(row.get("tokens_output") or 0))
    # Same interpretation as the local metering reader: both zero is the
    # legacy storage default, rather than a recorded token measurement.
    unrecorded_tokens = tokens_in == 0 and tokens_out == 0
    record = ModelMeteringRecord(
        correlation_id=str(row["correlation_id"]),
        occurred_at=occurred_at,
        # OMN-24615 AC6: the model that ran, the same order as the metering reader.
        model=str(row.get("model_name") or row.get("delegated_to") or ""),
        task_type=str(row.get("task_type") or ""),
        tokens_in=None if unrecorded_tokens else tokens_in,
        tokens_out=None if unrecorded_tokens else tokens_out,
        spend_usd=priced_spend(row),  # OMN-24615 AC3: a named price only
        # OMN-21264, OMN-24615 AC1: any stored failure cause, returned_to_caller
        # included, means the run saved nothing (ModelMeteringRecord.failed).
        failure_cause=str(row.get("terminal_failure_cause") or "") or None,
    )
    selection = sqlite_metering_summary.resolve_metering_baseline_model()
    baseline = baseline_pricing.resolve_baseline(selection.model)
    if baseline is None:
        return {**empty, "savings_measurement": "baseline_unresolved"}
    result = (
        HandlerMeteringSummary()
        .handle(
            ModelMeteringSummaryRequest(
                window=ModelMeteringWindow(label="run", end=occurred_at),
                records=(record,),
                baseline=baseline,
            )
        )
        .by_run[0]
    )
    return {
        "modelled_savings_usd": format(result.savings_usd, "f")
        if result.savings_usd is not None
        else None,
        "savings_baseline_model": result.baseline_model,
        "savings_pricing_manifest_version": result.pricing_manifest_version,
        "savings_measurement": result.measurement.value,
    }
