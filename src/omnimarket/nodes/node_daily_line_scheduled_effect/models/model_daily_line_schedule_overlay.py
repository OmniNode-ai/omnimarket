# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily line scheduled effect's cadence overlay (OMN-25163)."""

from __future__ import annotations

from pydantic import ConfigDict

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelDailyLineScheduleOverlay(ModelScheduleDeclaration):
    """The daily line's interval and grace window, with no additional settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")


__all__: list[str] = ["ModelDailyLineScheduleOverlay"]
