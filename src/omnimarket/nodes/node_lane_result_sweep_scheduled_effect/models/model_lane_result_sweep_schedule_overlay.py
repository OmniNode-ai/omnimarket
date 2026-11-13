# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the lane result sweep scheduled effect reads: the look-back each run sweeps."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelLaneResultSweepScheduleOverlay(ModelScheduleDeclaration):
    """The look-back window the sweep reads each run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(gt=0)


__all__: list[str] = [
    "ModelLaneResultSweepScheduleOverlay",
]
