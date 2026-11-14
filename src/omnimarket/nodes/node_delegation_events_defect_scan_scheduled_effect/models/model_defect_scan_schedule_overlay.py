# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the defect scan scheduled effect reads: which scan overlays to run."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelDefectScanScheduleOverlay(ModelScheduleDeclaration):
    """The overlay ids the scan's writing run reads: the effect's own and the compute node's scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scan_effect_overlay_id: str = Field(min_length=1)
    scan_compute_overlay_id: str = Field(min_length=1)


__all__: list[str] = [
    "ModelDefectScanScheduleOverlay",
]
