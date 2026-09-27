# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, deterministic fold for one topic-activity observation."""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_projection_topic_activity.models import (
    EnumTopicActivityState,
    ModelTopicActivityProjectionRequest,
    ModelTopicActivityProjectionResult,
    ModelTopicActivityRow,
)

TABLE_TOPIC_ACTIVITY = "topic_activity"
TOPIC_ACTIVITY_CONFLICT_KEY = "topic"


class HandlerProjectionTopicActivity:
    """Canonical definition-B fold: typed inputs, typed output, no I/O."""

    def handle(
        self, request: ModelTopicActivityProjectionRequest
    ) -> ModelTopicActivityProjectionResult:
        previous = request.previous_row
        sample = request.sample
        sampled_at = request.sampled_at
        if sample is None or not isinstance(sampled_at, datetime):
            if previous is not None:
                return ModelTopicActivityProjectionResult(row=previous, applied=False)
            return ModelTopicActivityProjectionResult(
                row=ModelTopicActivityRow(
                    topic=request.topic,
                    activity_state=EnumTopicActivityState.UNKNOWN,
                ),
                applied=False,
            )

        if previous is not None and previous.sampled_at is not None:
            if sampled_at <= previous.sampled_at:
                return ModelTopicActivityProjectionResult(row=previous, applied=False)
            elapsed = (sampled_at - previous.sampled_at).total_seconds()
            previous_high = previous.high_watermark_total
        else:
            elapsed = 0.0
            previous_high = None

        delta = (
            None
            if previous_high is None
            else sample.high_watermark_total - previous_high
        )
        rate = None if delta is None or elapsed <= 0 else delta / elapsed
        retained = sample.high_watermark_total - sample.low_watermark_total
        newest = sample.newest_message_at or (
            previous.newest_message_at if previous is not None else None
        )
        newest_age = (
            None if newest is None else max(0.0, (sampled_at - newest).total_seconds())
        )
        state = (
            EnumTopicActivityState.ACTIVE
            if sample.messages_last_hour > 0
            else EnumTopicActivityState.QUIET
        )
        row = ModelTopicActivityRow(
            topic=sample.topic,
            sampled_at=sampled_at,
            high_watermark_total=sample.high_watermark_total,
            low_watermark_total=sample.low_watermark_total,
            retained_messages=retained,
            messages_since_previous_sample=delta,
            rate_per_second=rate,
            messages_last_hour=sample.messages_last_hour,
            messages_last_24h=sample.messages_last_24h,
            rate_last_hour_per_second=sample.messages_last_hour / 3600,
            retention_truncated=sample.retention_truncated,
            newest_message_at=newest,
            newest_message_age_seconds_at_sample=newest_age,
            activity_state=state,
        )
        return ModelTopicActivityProjectionResult(row=row, applied=True)


__all__ = [
    "TABLE_TOPIC_ACTIVITY",
    "TOPIC_ACTIVITY_CONFLICT_KEY",
    "HandlerProjectionTopicActivity",
]
