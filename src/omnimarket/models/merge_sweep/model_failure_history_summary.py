# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Aggregate failure history stats for ModelSkillResult (OMN-23854)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelFailureHistorySummary(BaseModel):
    """Aggregate failure history stats for ModelSkillResult."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_tracked: int = 0
    stuck_prs: int = 0
    chronic_prs: int = 0
    recidivist_prs: int = 0
    cleaned_merged: int = 0


__all__ = ["ModelFailureHistorySummary"]
