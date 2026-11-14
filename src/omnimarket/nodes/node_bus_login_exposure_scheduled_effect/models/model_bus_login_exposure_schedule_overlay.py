# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the bus login exposure scheduled effect reads: which probe overlay to run."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelBusLoginExposureScheduleOverlay(ModelScheduleDeclaration):
    """The overlay id of the exposure probe's own overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exposure_overlay_id: str = Field(min_length=1)


__all__: list[str] = [
    "ModelBusLoginExposureScheduleOverlay",
]
