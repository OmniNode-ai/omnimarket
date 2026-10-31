# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReceiptGatePinFrictionReport - the per-pin-kind comparison over a window (OMN-20134).

Pure: rows in, one report out. The rows are the projection's
(``node_projection_receipt_gate_pin_friction``), read through the projection API or
the projection read node; the window selects the cohort by the time the updater
first met each pull request.
"""

from __future__ import annotations

from statistics import median
from typing import Literal

from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinFrictionReason,
    EnumPinKind,
    EnumPinPrState,
    EnumPinRefusalClass,
    ModelPinFrictionRow,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_report_compute.models import (
    ModelPinFrictionReport,
    ModelPinFrictionReportRequest,
    ModelPinKindSummary,
    ModelRefusalCount,
)


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def summarize_kind(
    kind: EnumPinKind, rows: tuple[ModelPinFrictionRow, ...]
) -> ModelPinKindSummary:
    """One pin kind's measures over its cohort rows."""
    opened = [r for r in rows if r.pr_opened_at is not None]
    merged = [r for r in rows if r.state is EnumPinPrState.MERGED]
    fix_pushes = sum(r.fix_pushes for r in opened)
    ci_reruns = sum(r.ci_reruns for r in opened)
    refusals = [refusal for r in rows for refusal in r.refusals]
    return ModelPinKindSummary(
        pin_kind=kind,
        prs_opened=len(opened),
        merged=len(merged),
        merged_untouched=sum(1 for r in merged if r.merged_untouched),
        still_open=sum(1 for r in rows if r.state is EnumPinPrState.OPENED),
        closed_unmerged=sum(1 for r in rows if r.state is EnumPinPrState.CLOSED),
        median_seconds_pr_open_to_merge=_median(
            [
                r.seconds_pr_open_to_merge
                for r in merged
                if r.seconds_pr_open_to_merge is not None
            ]
        ),
        median_seconds_tag_to_merge=_median(
            [
                r.seconds_tag_to_merge
                for r in merged
                if r.seconds_tag_to_merge is not None
            ]
        ),
        fix_pushes=fix_pushes,
        ci_reruns=ci_reruns,
        touches_per_pr=(None if not opened else (fix_pushes + ci_reruns) / len(opened)),
        refusals=len(refusals),
        refusals_by_class=tuple(
            ModelRefusalCount(
                refusal_class=c,
                count=sum(1 for r in refusals if r.refusal_class is c),
            )
            for c in EnumPinRefusalClass
            if any(r.refusal_class is c for r in refusals)
        ),
        updater_failures=sum(
            1
            for r in rows
            if EnumPinFrictionReason.UPDATER_FAILED in r.friction_reasons
        ),
    )


class HandlerReceiptGatePinFrictionReport:
    """COMPUTE handler: the sha and tag columns of the comparison."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelPinFrictionReportRequest) -> ModelPinFrictionReport:
        cohort = tuple(
            r
            for r in request.rows
            if request.window_start <= r.first_seen_at < request.window_end
        )
        return ModelPinFrictionReport(
            window_start=request.window_start,
            window_end=request.window_end,
            rows_in_window=len(cohort),
            kinds=tuple(
                summarize_kind(kind, tuple(r for r in cohort if r.pin_kind is kind))
                for kind in (EnumPinKind.SHA, EnumPinKind.TAG)
            ),
        )


__all__ = ["HandlerReceiptGatePinFrictionReport", "summarize_kind"]
