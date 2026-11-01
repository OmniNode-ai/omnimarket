# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build one metric from its sources, so an unread source is never filled with a zero."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from omnimarket.models.process_health import (
    EnumMetricKind,
    EnumPolarity,
    EnumReadStatus,
    EnumSourceName,
    ModelBreakdownItem,
    ModelMetric,
    ModelMetricSource,
    ModelSourceRead,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import (
    Window,
    combined_status,
    source_for_window,
)


@dataclass(frozen=True)
class Computed:
    """What a metric's calculation returns when every source covers the window."""

    value: float | None
    breakdown: tuple[ModelBreakdownItem, ...] = ()
    note: str = ""
    partial_reason: str = ""
    unread_reason: str = ""


@dataclass(frozen=True)
class MetricSpec:
    """The fixed description of one metric."""

    key: str
    label: str
    unit: str
    kind: EnumMetricKind
    polarity: EnumPolarity
    sources: tuple[EnumSourceName, ...]
    method: str


def round_value(value: float) -> float:
    """Three places, so the JSON is stable across platforms."""
    return round(value, 3)


def breakdown_of(counts: Mapping[str, float]) -> tuple[ModelBreakdownItem, ...]:
    """A sorted breakdown with rounded values."""
    return tuple(
        ModelBreakdownItem(key=key, value=round_value(counts[key]))
        for key in sorted(counts)
    )


def build_metric(
    spec: MetricSpec,
    reads: dict[EnumSourceName, ModelSourceRead],
    window: Window,
    compute: Callable[[], Computed],
) -> ModelMetric:
    """Compute the metric only when every source it names covers the window at least in part."""
    sources: tuple[ModelMetricSource, ...] = tuple(
        source_for_window(name, reads, window) for name in spec.sources
    )
    status = combined_status(sources)
    if status is EnumReadStatus.UNREAD:
        reasons = "; ".join(
            f"{source.name.value}: {source.reason}"
            for source in sources
            if source.status is EnumReadStatus.UNREAD
        )
        return ModelMetric(
            key=spec.key,
            label=spec.label,
            unit=spec.unit,
            kind=spec.kind,
            polarity=spec.polarity,
            value=None,
            status=status,
            sources=sources,
            method=spec.method,
            note=f"unread: {reasons}",
        )
    computed = compute()
    if computed.unread_reason:
        return ModelMetric(
            key=spec.key,
            label=spec.label,
            unit=spec.unit,
            kind=spec.kind,
            polarity=spec.polarity,
            value=None,
            status=EnumReadStatus.UNREAD,
            sources=sources,
            method=spec.method,
            note=f"unread: {computed.unread_reason}",
        )
    partial_reasons = "; ".join(
        f"{source.name.value}: {source.reason}"
        for source in sources
        if source.status is EnumReadStatus.PARTIAL
    )
    note = "; ".join(
        part
        for part in (computed.note, partial_reasons, computed.partial_reason)
        if part
    )
    if computed.partial_reason and status is EnumReadStatus.READ:
        status = EnumReadStatus.PARTIAL
    return ModelMetric(
        key=spec.key,
        label=spec.label,
        unit=spec.unit,
        kind=spec.kind,
        polarity=spec.polarity,
        value=None if computed.value is None else round_value(computed.value),
        status=status,
        sources=sources,
        method=spec.method,
        note=note,
        breakdown=computed.breakdown,
    )
