# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare one scheduled baseline run to the frozen baseline (OMN-22959).

A route reads WORKING only when it shows a timestamped command, a runtime result that
answers that command (same ``command_id``, observed no earlier than the command was
issued) and a comparison with no metric worse than the frozen baseline. Counts are
compared before times, in the baseline's own order within each kind. A metric the run
did not report reads ``missing`` and counts against the route: absence cannot show a
metric held. No result before the deadline reads UNKNOWN; after it, NOT WORKING.
Every NOT WORKING receipt carries one cause-lane request keyed to the run.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    EnumBaselineMetricKind,
    EnumBaselineOutcome,
    EnumBaselineVerdict,
    EnumBaselineWorseWhen,
    ModelBaselineCauseLaneRequest,
    ModelBaselineComparison,
    ModelFrozenBaselineMetric,
    ModelWorkflowBaselineCompareReceipt,
    ModelWorkflowBaselineCompareRequest,
)

_KIND_ORDER = (EnumBaselineMetricKind.COUNT, EnumBaselineMetricKind.TIME)


def _outcome(
    metric: ModelFrozenBaselineMetric, observed: float | None
) -> EnumBaselineOutcome:
    if observed is None:
        return EnumBaselineOutcome.MISSING
    # Decimal of the shortest repr keeps the inclusive boundary exact for decimal measurements.
    value, tolerance = Decimal(str(metric.value)), Decimal(str(metric.tolerance))
    reading = Decimal(str(observed))
    if metric.worse_when is EnumBaselineWorseWhen.LOWER:
        worse = reading < value - tolerance
    else:
        worse = reading > value + tolerance
    return EnumBaselineOutcome.WORSE if worse else EnumBaselineOutcome.OK


class HandlerWorkflowBaselineCompare:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(
        self, request: ModelWorkflowBaselineCompareRequest
    ) -> ModelWorkflowBaselineCompareReceipt:
        command, result = request.command, request.result
        common = {
            "route": command.route,
            "command_id": command.command_id,
            "command_issued_at": command.issued_at,
            "baseline_id": request.baseline.baseline_id,
            "compared_at": request.now,
            "result_command_id": result.command_id if result else None,
            "result_observed_at": result.observed_at if result else None,
        }
        deadline = command.issued_at + timedelta(
            seconds=request.result_deadline_seconds
        )
        if result is None:
            if request.now <= deadline:
                return self._receipt(
                    request,
                    common,
                    False,
                    (),
                    EnumBaselineVerdict.UNKNOWN,
                    "no runtime result yet; its deadline has not passed",
                )
            return self._not_working(
                request,
                common,
                False,
                (),
                "no runtime result by its deadline",
                ("runtime-result",),
            )
        if result.command_id != command.command_id:
            return self._not_working(
                request,
                common,
                False,
                (),
                f"the result answers command {result.command_id}, not {command.command_id}",
                ("correlation",),
            )
        if result.observed_at < command.issued_at:
            return self._not_working(
                request,
                common,
                False,
                (),
                "the result was observed before its command was issued",
                ("correlation",),
            )

        if result.observed_at > deadline:
            return self._not_working(
                request,
                common,
                True,
                (),
                "the result was observed after its deadline",
                ("runtime-result",),
            )

        observed = {r.name: r for r in result.readings}
        comparisons: list[ModelBaselineComparison] = []
        for kind in _KIND_ORDER:
            for metric in request.baseline.metrics:
                if metric.kind is not kind:
                    continue
                reading = observed.get(metric.name)
                value = reading.value if reading and reading.kind is kind else None
                comparisons.append(
                    ModelBaselineComparison(
                        name=metric.name,
                        kind=metric.kind,
                        worse_when=metric.worse_when,
                        tolerance=metric.tolerance,
                        baseline_value=metric.value,
                        observed_value=value,
                        outcome=_outcome(metric, value),
                    )
                )
        bad = tuple(c for c in comparisons if c.outcome is not EnumBaselineOutcome.OK)
        if bad:
            names = ", ".join(f"{c.name} {c.outcome.value}" for c in bad)
            return self._not_working(
                request,
                common,
                True,
                tuple(comparisons),
                f"worse than baseline {request.baseline.baseline_id}: {names}",
                tuple(c.name for c in bad),
            )
        return self._receipt(
            request,
            common,
            True,
            tuple(comparisons),
            EnumBaselineVerdict.WORKING,
            f"every metric holds against baseline {request.baseline.baseline_id}",
        )

    def _not_working(
        self,
        request: ModelWorkflowBaselineCompareRequest,
        common: dict[str, object],
        correlated: bool,
        comparisons: tuple[ModelBaselineComparison, ...],
        reason: str,
        subjects: tuple[str, ...],
    ) -> ModelWorkflowBaselineCompareReceipt:
        return self._receipt(
            request,
            common,
            correlated,
            comparisons,
            EnumBaselineVerdict.NOT_WORKING,
            reason,
            subjects,
        )

    def _receipt(
        self,
        request: ModelWorkflowBaselineCompareRequest,
        common: dict[str, object],
        correlated: bool,
        comparisons: tuple[ModelBaselineComparison, ...],
        verdict: EnumBaselineVerdict,
        reason: str,
        subjects: tuple[str, ...] = (),
    ) -> ModelWorkflowBaselineCompareReceipt:
        cause_lane = None
        if verdict is EnumBaselineVerdict.NOT_WORKING:
            cause_lane = ModelBaselineCauseLaneRequest(
                dispatch_key=":".join(
                    quote(part, safe="")
                    for part in (
                        request.command.route,
                        request.baseline.baseline_id,
                        request.command.command_id,
                    )
                ),
                route=request.command.route,
                command_id=request.command.command_id,
                baseline_id=request.baseline.baseline_id,
                subjects=subjects,
                cause=reason,
            )
        return ModelWorkflowBaselineCompareReceipt.model_validate(
            {
                **common,
                "correlated": correlated,
                "comparisons": comparisons,
                "verdict": verdict,
                "reason": reason,
                "cause_lane": cause_lane,
            }
        )


__all__: list[str] = ["HandlerWorkflowBaselineCompare"]
