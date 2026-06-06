# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelActionItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(description="Action item description")
    priority: str = Field(default="medium", description="Priority: high | medium | low")
    owner: str = Field(default="", description="Suggested owner role or team")


class ModelInsightsToPlanComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | error")
    plan: dict[str, object] = Field(
        default_factory=dict,
        description="Structured plan derived from extracted insights",
    )
    action_items: list[ModelActionItem] = Field(
        default_factory=list,
        description="Prioritised list of action items",
    )
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
