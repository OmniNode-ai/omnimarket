# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-local lane closeout sweep's settings, all of them an overlay's (OMN-23375)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneCloseoutSweepSettings(BaseModel):
    """Where a lab host keeps its lane run directories and worktrees, and when a lane counts as ended.

    The overlay ``node_lane_closeout_sweep_effect`` supplies every field; the node holds no default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_root: str = Field(
        description="Directory holding each launched lane's run directory and its .ctx context directory."
    )
    worktrees_dir: str = Field(
        description="Directory under the registry root holding one directory per lane."
    )
    records_dir: str = Field(
        description="Directory under the state directory where one closeout record per lane run is written."
    )
    ctx_suffix: str = Field(
        description="Suffix of a lane run's context directory name."
    )
    run_prefixes: tuple[str, ...] = Field(
        description="Prefixes a launcher puts before the lane name in its run directory name."
    )
    finished_markers: tuple[str, ...] = Field(
        description="Files whose presence in a context directory says the engine ended."
    )
    grace_seconds: int = Field(
        ge=0,
        description="How long after the engine ended the launcher may still read the run directory.",
    )
    orphan_age_seconds: int = Field(
        ge=0,
        description="Age at which a context directory with no end marker and no live process, or a lane directory with no context, counts as ended.",
    )
    max_lanes_per_sweep: int = Field(
        ge=1,
        description="Ended lane runs closed out in one sweep; the rest wait for the next tick.",
    )
    salvage_branch_prefix: str = Field(
        description="A dirty tree is committed to <prefix>/<tag> and pushed before it is removed."
    )
