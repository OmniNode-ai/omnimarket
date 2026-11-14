# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the C32 probe scheduled effect reads: which probe overlay to run."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelC32ProbeScheduleOverlay(ModelScheduleDeclaration):
    """The overlay id of the probe's own overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_overlay_id: str = Field(min_length=1)


__all__: list[str] = [
    "ModelC32ProbeScheduleOverlay",
]
