# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed WithinBudgetParams contract."""

from pydantic import BaseModel, ConfigDict, Field


class ModelWithinBudgetParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_wall_time_ms: int = Field(ge=1)
