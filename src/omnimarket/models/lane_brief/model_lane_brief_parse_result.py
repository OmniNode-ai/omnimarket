# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed brief, or the lines that refused it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_brief.model_lane_brief import ModelLaneBrief
from omnimarket.models.lane_brief.model_lane_brief_issue import ModelLaneBriefIssue


class ModelLaneBriefParseResult(BaseModel):
    """``brief`` is set exactly when ``parsed`` is true; ``issues`` is empty then."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed: bool
    brief: ModelLaneBrief | None = None
    issues: tuple[ModelLaneBriefIssue, ...] = ()
    parsed_at: datetime
