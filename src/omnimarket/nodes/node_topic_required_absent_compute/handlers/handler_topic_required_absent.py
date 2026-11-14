# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Computes the required-and-absent verdict for broker topics (OMN-19998).

The node reads the required topics and the live broker listing.
It carries no effect: it only reports what the contracts declare.
"""

from __future__ import annotations

from omnimarket.models.model_topic_required_absent import (
    EnumRequiredAbsentVerdict,
    ModelAbsentTopic,
    ModelRequiredAbsentReport,
    ModelRequiredAbsentRequest,
)


class HandlerTopicRequiredAbsent:
    """Handles the required-and-absent computation for broker topics."""

    def handle(self, request: ModelRequiredAbsentRequest) -> ModelRequiredAbsentReport:
        """Computes the required-and-absent verdict for broker topics."""
        required_topics = {item.topic for item in request.required}
        required_count = len(required_topics)

        if request.live_topics is None:
            return ModelRequiredAbsentReport(
                verdict=EnumRequiredAbsentVerdict.UNKNOWN,
                reason="the broker topic list was not read",
                required_count=required_count,
                present_count=0,
                absent=(),
            )

        live_set = set(request.live_topics)
        live_count = len(live_set)

        if live_count < request.rule.min_live_topics:
            return ModelRequiredAbsentReport(
                verdict=EnumRequiredAbsentVerdict.UNKNOWN,
                reason=f"the broker topic list holds {live_count} topics, fewer than the minimum {request.rule.min_live_topics}: a failed read, not an empty broker",
                required_count=required_count,
                present_count=live_count,
                absent=(),
            )

        present_count = len(required_topics & live_set)
        absent_topics = required_topics - live_set

        if not absent_topics:
            return ModelRequiredAbsentReport(
                verdict=EnumRequiredAbsentVerdict.WORKING,
                reason="every required topic exists on the broker",
                required_count=required_count,
                present_count=present_count,
                absent=(),
            )

        absent_entries = []
        for topic in sorted(absent_topics):
            required_by = sorted(
                {item.required_by for item in request.required if item.topic == topic}
            )
            absent_entries.append(
                ModelAbsentTopic(topic=topic, required_by=tuple(required_by))
            )

        return ModelRequiredAbsentReport(
            verdict=EnumRequiredAbsentVerdict.NOT_WORKING,
            reason=f"{len(absent_topics)} required topics are absent from the broker",
            required_count=required_count,
            present_count=present_count,
            absent=tuple(absent_entries),
        )
