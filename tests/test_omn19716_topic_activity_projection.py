# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19716 pure topic-activity fold tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.events.topic_activity import ModelTopicActivitySample
from omnimarket.nodes.node_projection_topic_activity.handlers.handler_projection_topic_activity import (
    HandlerProjectionTopicActivity,
)
from omnimarket.nodes.node_projection_topic_activity.models import (
    EnumTopicActivityState,
    ModelTopicActivityProjectionRequest,
)

pytestmark = pytest.mark.unit
_T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _sample(
    *, high: int, low: int = 10, last_hour: int = 0, last_day: int = 0
) -> ModelTopicActivitySample:
    return ModelTopicActivitySample(
        topic="onex.evt.example.v1",
        high_watermark_total=high,
        low_watermark_total=low,
        messages_last_hour=last_hour,
        messages_last_24h=last_day,
        retention_truncated=False,
        newest_message_at=_T0 - timedelta(seconds=5),
    )


def test_fold_is_pure_and_replay_deterministic() -> None:
    request = ModelTopicActivityProjectionRequest(
        topic="onex.evt.example.v1",
        sampled_at=_T0,
        sample=_sample(high=40, last_hour=8, last_day=20),
    )
    handler = HandlerProjectionTopicActivity()
    assert handler.handle(request) == handler.handle(request)


def test_first_sample_has_null_delta_and_rate_not_zero() -> None:
    result = HandlerProjectionTopicActivity().handle(
        ModelTopicActivityProjectionRequest(
            topic="onex.evt.example.v1",
            sampled_at=_T0,
            sample=_sample(high=40, last_hour=8, last_day=20),
        )
    )
    assert result.row.messages_since_previous_sample is None
    assert result.row.rate_per_second is None
    assert result.row.activity_state is EnumTopicActivityState.ACTIVE


def test_two_samples_compute_delta_rate_and_one_hour_rate() -> None:
    handler = HandlerProjectionTopicActivity()
    first = handler.handle(
        ModelTopicActivityProjectionRequest(
            topic="onex.evt.example.v1",
            sampled_at=_T0,
            sample=_sample(high=100, last_hour=10, last_day=50),
        )
    ).row
    second = handler.handle(
        ModelTopicActivityProjectionRequest(
            topic="onex.evt.example.v1",
            sampled_at=_T0 + timedelta(seconds=10),
            previous_row=first,
            sample=_sample(high=130, last_hour=18, last_day=70),
        )
    ).row
    assert second.messages_since_previous_sample == 30
    assert second.rate_per_second == 3.0
    assert second.messages_last_hour == 18
    assert second.rate_last_hour_per_second == 18 / 3600


def test_duplicate_part_is_idempotent_and_keeps_the_previous_row() -> None:
    handler = HandlerProjectionTopicActivity()
    request = ModelTopicActivityProjectionRequest(
        topic="onex.evt.example.v1",
        sampled_at=_T0,
        sample=_sample(high=40, last_hour=8, last_day=20),
    )
    first = handler.handle(request).row
    replay = handler.handle(request.model_copy(update={"previous_row": first}))
    assert replay.applied is False
    assert replay.row == first


def test_never_sampled_topic_is_unknown_with_null_counters() -> None:
    row = (
        HandlerProjectionTopicActivity()
        .handle(ModelTopicActivityProjectionRequest(topic="empty.topic"))
        .row
    )
    assert row.activity_state is EnumTopicActivityState.UNKNOWN
    assert row.high_watermark_total is None
    assert row.messages_last_hour is None
    assert row.rate_per_second is None


def test_absent_is_a_distinct_operator_facing_state() -> None:
    assert EnumTopicActivityState.ABSENT.value == "ABSENT"
