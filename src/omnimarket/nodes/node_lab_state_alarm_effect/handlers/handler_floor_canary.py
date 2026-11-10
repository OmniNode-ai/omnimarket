# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hosted canary's verdict (OMN-19998, 17a; plan figure W23).

Pure: ``handle(ModelCanaryInput) -> ModelCanaryVerdict``. The canary runs outside the lab and reads two things the lab
publishes: the generated time of the served floor record, and the alarm path's heartbeat. Either one older than its
bound (or absent) fails the canary, and a failure is a ``notify``. The caller delivers the notice through a channel
that does not depend on the lab; this node only decides.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.floor_17a import ModelCanaryInput, ModelCanaryVerdict


def _age(now: datetime, moment: datetime | None) -> int | None:
    return None if moment is None else max(int((now - moment).total_seconds()), 0)


class HandlerFloorCanary:
    def handle(self, request: ModelCanaryInput) -> ModelCanaryVerdict:
        floor_age = _age(request.now, request.record_generated_at)
        beat_age = _age(request.now, request.heartbeat_at)
        floor_silent = floor_age is None or floor_age > request.floor_max_age_s
        alarm_silent = beat_age is None or beat_age > request.heartbeat_max_age_s
        publish_age = _age(request.now, request.published_at)
        publisher_silent = request.check_publisher and (
            publish_age is None or publish_age > request.floor_max_age_s
        )
        if publisher_silent:
            state = "publisher_silent"
        elif floor_silent and alarm_silent:
            state = "both_silent"
        elif floor_silent:
            state = "floor_silent"
        elif alarm_silent:
            state = "alarm_silent"
        else:
            state = "ok"
        parts = []
        if publisher_silent:
            parts.append(
                "nothing has been published for the canary "
                + ("ever" if publish_age is None else f"in {publish_age // 60} min")
            )
        if floor_silent:
            parts.append(
                "the floor record is "
                + ("absent" if floor_age is None else f"{floor_age // 60} min old")
            )
        if alarm_silent:
            parts.append(
                "the alarm path has "
                + (
                    "never beaten"
                    if beat_age is None
                    else f"been silent for {beat_age // 60} min"
                )
            )
        return ModelCanaryVerdict(
            state=state,
            notify=state != "ok",
            message="; ".join(parts)
            if parts
            else "the floor and the alarm path are alive",
            floor_age_s=floor_age,
            heartbeat_age_s=beat_age,
        )
