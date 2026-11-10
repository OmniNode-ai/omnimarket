# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Count each census class in every rolling 24 hour window of a span: the measured input of a freeze."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta

from omnimarket.models.model_andon_threshold import (
    ModelClassWindowCounts,
    ModelWindowMeasureRequest,
    ModelWindowMeasureResult,
)


class HandlerWindowMeasure:
    """Windows overlap (they start every ``step_hours``), so a burst across a boundary is counted whole.

    A window ``[start, start + window_hours)`` counts the events with ``start <= t < start + window_hours``.
    Windows are those that end inside the span; an event outside the span is counted aside, never measured.
    """

    def handle(self, request: ModelWindowMeasureRequest) -> ModelWindowMeasureResult:
        window = timedelta(hours=request.window_hours)
        step = timedelta(hours=request.step_hours)
        starts: list[datetime] = []
        start = request.measured_from
        while start + window <= request.measured_until:
            starts.append(start)
            start += step
        times_by_class: dict[str, list[datetime]] = {}
        outside = 0
        for event in request.events:
            if not request.measured_from <= event.observed_at < request.measured_until:
                outside += 1
                continue
            times_by_class.setdefault(event.class_id, []).append(event.observed_at)
        classes = []
        for class_id in sorted(times_by_class):
            times = sorted(times_by_class[class_id])
            counts = tuple(
                bisect_left(times, begin + window) - bisect_left(times, begin)
                for begin in starts
            )
            classes.append(
                ModelClassWindowCounts(class_id=class_id, window_counts=counts)
            )
        return ModelWindowMeasureResult(
            classes=tuple(classes),
            windows_measured=len(starts),
            events_read=len(request.events) - outside,
            events_outside_span=outside,
        )
