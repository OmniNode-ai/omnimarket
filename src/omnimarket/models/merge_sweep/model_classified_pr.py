# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR with its classification track (OMN-23854)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.merge_sweep.enum_pr_track import EnumPRTrack
from omnimarket.models.merge_sweep.model_pr_info import ModelPRInfo


class ModelClassifiedPR(BaseModel):
    """A PR with its classification track."""

    model_config = ConfigDict(extra="forbid")

    pr: ModelPRInfo
    track: EnumPRTrack
    reason: str
    failure_categories: list[str] = Field(default_factory=list)


__all__ = ["ModelClassifiedPR"]
