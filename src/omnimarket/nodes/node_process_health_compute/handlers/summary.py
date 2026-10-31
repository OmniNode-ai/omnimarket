# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The before and after summary over equal-length windows around the reset."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from omnimarket.models.process_health import (
    EnumMetricKind,
    EnumPolarity,
    EnumReadStatus,
    EnumVerdict,
    ModelMetric,
    ModelMetricComparison,
    ModelResetSummary,
    ModelWindowMetrics,
)
from omnimarket.nodes.node_process_health_compute.handlers.context import Context
from omnimarket.nodes.node_process_health_compute.handlers.metric_builder import (
    round_value,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import Window

WindowCalculator = Callable[[Window], ModelWindowMetrics]

_PER_DAY = frozenset(
    {EnumMetricKind.COUNT, EnumMetricKind.HOURS, EnumMetricKind.MINUTES}
)
_DAY_HOURS = 24.0


def comparable_value(metric: ModelMetric, window_hours: float) -> float | None:
    """A count or a duration as a rate per day; a ratio or a median as itself."""
    if metric.value is None or metric.status is EnumReadStatus.UNREAD:
        return None
    if metric.kind in _PER_DAY:
        return metric.value / window_hours * _DAY_HOURS if window_hours > 0 else None
    return metric.value


def _verdict(
    ctx: Context, polarity: EnumPolarity, before: float, after: float
) -> tuple[EnumVerdict, float | None]:
    delta = after - before
    ratio = delta / before if before != 0 else None
    if polarity is EnumPolarity.NEUTRAL:
        return EnumVerdict.INFORMATIONAL, ratio
    if delta == 0 or (
        ratio is not None and abs(ratio) <= ctx.config.flat_threshold_ratio
    ):
        return EnumVerdict.FLAT, ratio
    better = delta < 0 if polarity is EnumPolarity.LOWER_IS_BETTER else delta > 0
    return (EnumVerdict.IMPROVED if better else EnumVerdict.WORSE), ratio


def _compare(
    ctx: Context,
    before: ModelMetric,
    after: ModelMetric,
    before_hours: float,
    after_hours: float,
) -> ModelMetricComparison:
    left = comparable_value(before, before_hours)
    right = comparable_value(after, after_hours)
    unit = f"{before.unit} per day" if before.kind in _PER_DAY else before.unit
    notes: list[str] = []
    for side, metric in (("before", before), ("after", after)):
        if metric.status is not EnumReadStatus.READ:
            notes.append(f"{side}: {metric.status.value}; {metric.note}")
    if left is None or right is None:
        return ModelMetricComparison(
            key=before.key,
            label=before.label,
            unit=before.unit,
            kind=before.kind,
            polarity=before.polarity,
            before=None if left is None else round_value(left),
            after=None if right is None else round_value(right),
            delta=None,
            delta_ratio=None,
            verdict=EnumVerdict.UNMEASURED,
            before_status=before.status,
            after_status=after.status,
            comparable_unit=unit,
            note="; ".join(notes),
        )
    verdict, ratio = _verdict(ctx, before.polarity, left, right)
    return ModelMetricComparison(
        key=before.key,
        label=before.label,
        unit=before.unit,
        kind=before.kind,
        polarity=before.polarity,
        before=round_value(left),
        after=round_value(right),
        delta=round_value(right - left),
        delta_ratio=None if ratio is None else round_value(ratio),
        verdict=verdict,
        before_status=before.status,
        after_status=after.status,
        comparable_unit=unit,
        note="; ".join(notes),
    )


def build_summary(
    ctx: Context,
    window_metrics: WindowCalculator,
) -> ModelResetSummary:
    """Compare [reset - L, reset) with [reset, reset + L), L the shorter of the contract's maximum and the time since the reset."""
    config = ctx.config
    available = (ctx.request.generated_at - config.reset_at).total_seconds() / 3600.0
    length = min(config.summary_max_hours, available)
    if length <= 0:
        return ModelResetSummary(
            reset_at=config.reset_at,
            reset_label=config.reset_label,
            measured=False,
            reason="no time has passed since the reset at the generated-at time",
            window_hours=0.0,
            before=None,
            after=None,
            comparisons=(),
            improved=0,
            worse=0,
            flat=0,
            unmeasured=0,
        )
    span = timedelta(hours=length)
    before_window = Window(config.reset_at - span, config.reset_at)
    after_window = Window(config.reset_at, config.reset_at + span)
    before = window_metrics(before_window)
    after = window_metrics(after_window)
    after_by_key = {metric.key: metric for metric in after.metrics}
    comparisons = tuple(
        _compare(ctx, metric, after_by_key[metric.key], before.hours, after.hours)
        for metric in before.metrics
        if metric.key in after_by_key
    )
    counts = dict.fromkeys(EnumVerdict, 0)
    for comparison in comparisons:
        counts[comparison.verdict] += 1
    return ModelResetSummary(
        reset_at=config.reset_at,
        reset_label=config.reset_label,
        measured=True,
        reason="",
        window_hours=round_value(length),
        before=before,
        after=after,
        comparisons=comparisons,
        improved=counts[EnumVerdict.IMPROVED],
        worse=counts[EnumVerdict.WORSE],
        flat=counts[EnumVerdict.FLAT],
        unmeasured=counts[EnumVerdict.UNMEASURED],
    )
