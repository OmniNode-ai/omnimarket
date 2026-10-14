# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B fold, reusing the canonical metering computation."""

import json
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
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

_MICRO_USD = Decimal("0.000001")


def savings_per_measured_run(
    savings_usd: Decimal | None, runs_measured: int
) -> str | None:
    """The average saving of a measured run, as decimal text to the millionth.

    Only measured runs are summed into savings_usd, so only they divide it. A
    row with no saving (no measured run, or no resolved baseline) has no
    average either: null, never zero.
    """
    if savings_usd is None or runs_measured <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 28
        quotient = (savings_usd / Decimal(runs_measured)).quantize(
            _MICRO_USD, rounding=ROUND_HALF_EVEN
        )
    return str(quotient)


def savings_pct_of_counterfactual(
    savings_usd: Decimal | None, counterfactual_usd: Decimal | None
) -> str | None:
    """Savings as a share of what the baseline would have cost, to the millionth.

    0.42 means 42% below the baseline. It is a ratio of two figures already on
    the row, written here so the dashboard never divides money. With no saving,
    no counterfactual, or a counterfactual of 0 there is nothing to compare
    against: null, never zero. A negative saving (spend above the baseline)
    stays negative.
    """
    if savings_usd is None or counterfactual_usd is None or counterfactual_usd == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 28
        share = (savings_usd / counterfactual_usd).quantize(
            _MICRO_USD, rounding=ROUND_HALF_EVEN
        )
    return str(share)


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
                    savings_per_measured_run_usd=savings_per_measured_run(
                        summary.savings_usd, summary.runs_measured
                    ),
                    savings_pct_of_counterfactual=savings_pct_of_counterfactual(
                        summary.savings_usd, summary.counterfactual_usd
                    ),
                    # OMN-20226: no metering record carries a raw or compressed
                    # token count or a semantic-cache answer, so there is
                    # nothing to divide: null, never 0 or 1.00x.
                    compression_ratio=None,
                    cache_hit_rate=None,
                    runs_cache_answered=None,
                    summary_json=json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ),
                )
            )
        return ModelMeteringSummaryFoldResult(rows=tuple(rows))
