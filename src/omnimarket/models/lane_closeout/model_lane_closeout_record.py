# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable closeout record of one ended lane run, written on the host the lane ran on."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_closeout.model_lane_closeout_tree_outcome import (
    ModelLaneCloseoutTreeOutcome,
)


class ModelLaneCloseoutRecord(BaseModel):
    """A lane run is closed out when its record says so: trees removed or kept with a reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    run: str  # the run directory's name, or the lane name for a lane directory with no run
    host: str
    closed_at: str
    ended_by: str  # the end marker's file name, or "orphan-age" / "orphan-lane-dir"
    trees: tuple[ModelLaneCloseoutTreeOutcome, ...] = Field(default=())
    run_dir_removed: bool
    complete: bool  # every tree gone and the run directory removed
    error: str = Field(
        default="",
        description="Why the closeout stopped early, empty when it ran through.",
    )
