# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule declaration for the overlay consumer join check."""

from __future__ import annotations

from pydantic import ConfigDict

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelOverlayDriftComputeScheduleOverlay(ModelScheduleDeclaration):
    """The schedule for the fixed overlay consumer join check."""

    model_config = ConfigDict(frozen=True, extra="forbid")


__all__: list[str] = [
    "ModelOverlayDriftComputeScheduleOverlay",
]
