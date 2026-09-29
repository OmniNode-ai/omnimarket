# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B fold, reusing the canonical metering computation."""

import json
from datetime import UTC, datetime, time, timedelta
from typing import Literal

from omnimarket.nodes.node_metering_summary_compute import (
    HandlerMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)
from omnimarket.nodes.node_projection_metering_summary.models import (
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryFoldResult,
    ModelMeteringSummaryRow,
)


class HandlerProjectionMeteringSummary:
    """Fold a complete recorded snapshot into daily and all-time rows."""

    def handle(
        self, request: ModelMeteringSummaryFoldRequest
    ) -> ModelMeteringSummaryFoldResult:
        as_of = request.as_of.astimezone(UTC)
        records = tuple(
            sorted(
                (r for r in request.records if r.occurred_at < as_of),
                key=lambda r: (r.occurred_at, r.correlation_id, r.model_dump_json()),
            )
        )
        days = (
            request.days
            if request.days is not None
            else {r.occurred_at.astimezone(UTC).date() for r in records}
        )
        windows: list[tuple[Literal["all", "day"], str, datetime | None, datetime]] = [
            ("all", "", None, as_of)
        ]
        for day in sorted(days):
            day_start = datetime.combine(day, time.min, tzinfo=UTC)
            windows.append(
                ("day", day.isoformat(), day_start, day_start + timedelta(days=1))
            )
        rows = []
        for kind, key, start, end in windows:
            selected = tuple(
                r
                for r in records
                if (start is None or r.occurred_at >= start) and r.occurred_at < end
            )
            summary = HandlerMeteringSummary().handle(
                ModelMeteringSummaryRequest(
                    window=ModelMeteringWindow(label=kind, start=start, end=end),
                    records=selected,
                    baseline=request.baseline,
                    top_models=max(1, len(selected)),
                )
            )
            payload = summary.model_dump(mode="json")
            rows.append(
                ModelMeteringSummaryRow(
                    tenant_id=request.tenant_id,
                    window_kind=kind,
                    window_start=key,
                    window_end=end.isoformat(),
                    as_of=as_of.isoformat(),
                    baseline_model=request.baseline_model,
                    pricing_manifest_version=request.baseline.pricing_manifest_version
                    if request.baseline
                    else None,
                    baseline_state=summary.baseline_state,
                    runs_total=summary.runs_total,
                    runs_measured=summary.runs_measured,
                    runs_unknown_tokens=summary.runs_unknown_tokens,
                    runs_unknown_spend=summary.runs_unknown_spend,
                    tokens_in=summary.tokens_in,
                    tokens_out=summary.tokens_out,
                    spend_usd=payload["spend_usd"],
                    counterfactual_usd=payload["counterfactual_usd"],
                    savings_usd=payload["savings_usd"],
                    summary_json=json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ),
                )
            )
        return ModelMeteringSummaryFoldResult(rows=tuple(rows))
