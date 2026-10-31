# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A time window and how much of it one source covered."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omnimarket.models.process_health import (
    EnumReadStatus,
    EnumSourceName,
    ModelMetricSource,
    ModelSourceRead,
)

_EPSILON_HOURS = 1e-6


@dataclass(frozen=True)
class Window:
    """A half-open span [start, end)."""

    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def overlap_hours(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    """Hours the two spans share."""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def covered_hours(read: ModelSourceRead, window: Window) -> float:
    """Hours of the window the source recorded: its span less its declared gaps."""
    if (
        read.status is EnumReadStatus.UNREAD
        or read.covered_from is None
        or read.covered_to is None
    ):
        return 0.0
    hours = overlap_hours(window.start, window.end, read.covered_from, read.covered_to)
    start = max(window.start, read.covered_from)
    end = min(window.end, read.covered_to)
    for gap in read.gaps:
        hours -= overlap_hours(start, end, gap.start, gap.end)
    return max(0.0, hours)


def source_for_window(
    name: EnumSourceName,
    reads: dict[EnumSourceName, ModelSourceRead],
    window: Window,
) -> ModelMetricSource:
    """The metric-level view of one source over one window."""
    read = reads.get(name)
    if read is None:
        return ModelMetricSource(
            name=name,
            status=EnumReadStatus.UNREAD,
            covered_hours=0.0,
            reason="the caller declared no read of this source",
        )
    if read.status is EnumReadStatus.UNREAD:
        return ModelMetricSource(
            name=name,
            status=EnumReadStatus.UNREAD,
            covered_hours=0.0,
            reason=read.reason,
        )
    hours = covered_hours(read, window)
    if hours <= _EPSILON_HOURS:
        return ModelMetricSource(
            name=name,
            status=EnumReadStatus.UNREAD,
            covered_hours=0.0,
            reason=f"the source does not cover this window ({read.reason or 'outside its span'})",
        )
    if hours >= window.hours - _EPSILON_HOURS:
        return ModelMetricSource(
            name=name, status=EnumReadStatus.READ, covered_hours=round(hours, 3)
        )
    return ModelMetricSource(
        name=name,
        status=EnumReadStatus.PARTIAL,
        covered_hours=round(hours, 3),
        reason=f"covers {hours:.1f} of {window.hours:.1f} hours of the window",
    )


def combined_status(sources: tuple[ModelMetricSource, ...]) -> EnumReadStatus:
    """Unread when any source is unread, partial when any is partial, else read."""
    statuses = {source.status for source in sources}
    if EnumReadStatus.UNREAD in statuses:
        return EnumReadStatus.UNREAD
    if EnumReadStatus.PARTIAL in statuses:
        return EnumReadStatus.PARTIAL
    return EnumReadStatus.READ
