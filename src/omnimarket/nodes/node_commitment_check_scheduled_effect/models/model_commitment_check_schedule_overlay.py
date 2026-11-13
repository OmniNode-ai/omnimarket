# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the commitment check scheduled effect reads: which runner overlay to run."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelCommitmentCheckScheduleOverlay(ModelScheduleDeclaration):
    """The overlay id of the runner's own overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_overlay_id: str = Field(min_length=1)


__all__: list[str] = [
    "ModelCommitmentCheckScheduleOverlay",
]
