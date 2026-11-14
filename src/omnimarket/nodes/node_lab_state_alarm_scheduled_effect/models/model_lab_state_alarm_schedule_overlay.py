# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the lab state alarm scheduled effect reads: which alarm overlay to run."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelLabStateAlarmScheduleOverlay(ModelScheduleDeclaration):
    """The overlay of the alarm's own overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm_overlay_id: str = Field(min_length=1)


__all__: list[str] = [
    "ModelLabStateAlarmScheduleOverlay",
]
