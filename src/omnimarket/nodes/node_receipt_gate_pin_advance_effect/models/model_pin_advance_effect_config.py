# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect's contract-declared settings (OMN-20134)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPinAdvanceMode(StrEnum):
    """dry_run plans and reports; enforce publishes the branches and opens the pull requests."""

    DRY_RUN = "dry_run"
    ENFORCE = "enforce"


class ModelPinAdvanceEffectConfig(BaseModel):
    """``pin_advance_effect_config`` of this node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumPinAdvanceMode
    source_clone_root_env: str = Field(
        min_length=1,
        description="The environment variable naming the directory that holds the "
        "source repository's clone, in a directory of the repository's own name.",
    )
    fetch_tags: bool
    bus_lane_env: str = Field(min_length=1)
    default_bus_lane: str = Field(min_length=1)
    bus_timeout_s: float = Field(gt=0)
