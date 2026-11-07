# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The friction rollup step of the floor publish: its contract settings and the class-rule overlay (OMN-22478)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.friction_rollup import (
    ModelFrictionClassOwner,
    ModelFrictionClassRule,
)


class ModelFrictionRollupStepSettings(BaseModel):
    """What the rollup step reads and how it windows it; every value is the contract's, none is in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_path_env: str = Field(min_length=1)
    archive_subdir: str = Field(min_length=1)
    window_hours: int = Field(gt=0)
    bucket_hours: int = Field(gt=0)
    umbrella_tickets: tuple[str, ...]
    class_overlay_id: str = Field(min_length=1)
    class_overlay_file_env: str = Field(min_length=1)


class ModelFrictionClassOverlay(BaseModel):
    """The overlay home's class rules and class owners, the shape of the rollup node's ``overlay.example.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_rules: tuple[ModelFrictionClassRule, ...]
    class_owners: tuple[ModelFrictionClassOwner, ...] = ()
