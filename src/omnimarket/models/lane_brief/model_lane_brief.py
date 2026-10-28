# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane brief as its column-zero metadata lines state it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_brief.enum_lane_engine import EnumLaneEngine
from omnimarket.models.lane_brief_draft.model_brief_quoted_criteria import (
    ModelBriefQuotedCriteria,
)


class ModelLaneBrief(BaseModel):
    """``engine`` is the only required metadata line; ``required_host`` and ``proves`` are empty when the brief has none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(min_length=1)
    engine: EnumLaneEngine
    required_host: str = ""
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    opens_pr_in: str = Field(min_length=1)
    proves: str = ""
    quoted_criteria: tuple[ModelBriefQuotedCriteria, ...] = ()
