# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 7 (OMN-22942): the measured peaks that a freeze is computed from."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_andon_threshold import (
    ModelAndonThresholdFreeze,
    ModelClassEvent,
    ModelMeasurementProvenance,
    ModelThresholdComputeRequest,
    ModelWindowMeasureRequest,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_threshold_compute import (
    HandlerThresholdCompute,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_window_measure import (
    HandlerWindowMeasure,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def _request(
    events: tuple[ModelClassEvent, ...], *, days: int = 3, step_hours: int = 1
) -> ModelWindowMeasureRequest:
    return ModelWindowMeasureRequest(
        events=events,
        measured_from=T0,
        measured_until=T0 + timedelta(days=days),
        step_hours=step_hours,
    )


def _burst_across_a_day_boundary() -> tuple[ModelClassEvent, ...]:
    # Three at 23:00 and three at 01:00 the next day: no calendar day holds more than three,
    # but one rolling 24 hour window holds all six.
    return tuple(
        ModelClassEvent(class_id="hook.refusal", observed_at=_at(hour))
        for hour in (23.0, 23.2, 23.4, 25.0, 25.2, 25.4)
    )


def test_freeze_hash_peak_counts_a_burst_that_straddles_a_day_boundary() -> None:
    measured = HandlerWindowMeasure().handle(_request(_burst_across_a_day_boundary()))
    counts = {c.class_id: c.window_counts for c in measured.classes}
    assert max(counts["hook.refusal"]) == 6
    assert measured.windows_measured == len(counts["hook.refusal"])


def test_freeze_hash_failing_control_calendar_day_windows_miss_the_straddling_peak() -> (
    None
):
    # The control: windows started every 24 hours from midnight (calendar days) read a peak of 3.
    tumbling = HandlerWindowMeasure().handle(
        _request(_burst_across_a_day_boundary(), step_hours=24)
    )
    rolling = HandlerWindowMeasure().handle(
        _request(_burst_across_a_day_boundary(), step_hours=1)
    )
    assert max(tumbling.classes[0].window_counts) == 3
    assert max(rolling.classes[0].window_counts) == 6


def test_freeze_hash_window_is_half_open_and_events_outside_the_span_are_aside() -> (
    None
):
    events = (
        ModelClassEvent(class_id="a", observed_at=_at(0)),
        ModelClassEvent(
            class_id="a", observed_at=_at(24)
        ),  # next window, not the first
        ModelClassEvent(class_id="a", observed_at=_at(-1)),  # before the span
        ModelClassEvent(class_id="a", observed_at=_at(72)),  # at the end: outside
    )
    measured = HandlerWindowMeasure().handle(_request(events, step_hours=24))
    assert measured.classes[0].window_counts == (1, 1, 0)
    assert measured.events_read == 2
    assert measured.events_outside_span == 2


def test_freeze_hash_a_quiet_window_counts_zero_and_a_span_under_a_window_is_refused() -> (
    None
):
    events = (ModelClassEvent(class_id="a", observed_at=_at(1)),)
    measured = HandlerWindowMeasure().handle(_request(events, step_hours=24))
    assert measured.classes[0].window_counts == (1, 0, 0)
    with pytest.raises(ValidationError, match="shorter than one"):
        ModelWindowMeasureRequest(
            events=events,
            measured_from=T0,
            measured_until=T0 + timedelta(hours=23),
            step_hours=1,
        )


def test_freeze_hash_naive_timestamps_are_refused() -> None:
    naive = ModelClassEvent(class_id="a", observed_at=datetime(2026, 11, 1, 5, 0))
    with pytest.raises(ValidationError, match="no timezone"):
        _request((naive,))


def test_freeze_hash_measured_windows_feed_the_frozen_threshold_with_provenance() -> (
    None
):
    measured = HandlerWindowMeasure().handle(_request(_burst_across_a_day_boundary()))
    provenance = ModelMeasurementProvenance(
        source="test census",
        measured_from=T0,
        measured_until=T0 + timedelta(days=3),
        step_hours=1,
        events_read=measured.events_read,
    )
    result = HandlerThresholdCompute().handle(
        ModelThresholdComputeRequest(
            classes=measured.classes,
            recalibrated_on=date(2026, 11, 9),
            measurement=provenance,
        )
    )
    assert result.freeze.thresholds == {"hook.refusal": 6}
    document = yaml.safe_load(result.overlay_yaml)
    assert document["measurement"]["events_read"] == 6
    # The overlay text round-trips through the freeze model, hash included.
    assert ModelAndonThresholdFreeze(**document).thresholds == {"hook.refusal": 6}


def test_freeze_hash_provenance_is_outside_the_hash_and_a_changed_value_still_fails() -> (
    None
):
    measured = HandlerWindowMeasure().handle(_request(_burst_across_a_day_boundary()))
    document = yaml.safe_load(
        HandlerThresholdCompute()
        .handle(
            ModelThresholdComputeRequest(
                classes=measured.classes, recalibrated_on=date(2026, 11, 9)
            )
        )
        .overlay_yaml
    )
    document["measurement"] = {
        "source": "edited",
        "measured_from": T0.isoformat(),
        "measured_until": (T0 + timedelta(days=2)).isoformat(),
        "step_hours": 2,
        "events_read": 1,
    }
    ModelAndonThresholdFreeze(**document)  # provenance alone changes no value: accepted
    document["thresholds"] = {"hook.refusal": 60}  # a value with the old hash: refused
    with pytest.raises(ValidationError, match="threshold_hash"):
        ModelAndonThresholdFreeze(**document)
