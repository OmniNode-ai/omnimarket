# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane guard's end-record settings, all of them an overlay's."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneGuardEndRecordSettings(BaseModel):
    """Where the guard writes its kill records and how far it may read the dead lane's trees.

    The overlay ``node_lane_guard_unit_effect`` supplies every field; the node holds no default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_dir: str = Field(
        description="Absolute directory the guard writes <unit>.json into."
    )
    worktrees_dir: str = Field(
        description="Absolute directory holding one directory per lane, each holding the lane's trees."
    )
    git_timeout_s: int = Field(
        ge=1, description="Seconds one read-only git command may run."
    )
    max_trees: int = Field(ge=1, description="Trees read per record.")
