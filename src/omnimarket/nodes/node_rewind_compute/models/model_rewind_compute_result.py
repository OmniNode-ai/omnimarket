# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRewindComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | not_found | error")
    events: list[dict[str, object]] = Field(
        default_factory=list,
        description="Matching events within the window, ordered by timestamp ascending",
    )
    actions_taken: list[str] = Field(
        default_factory=list,
        description="Human-readable summary of actions the agent took",
    )
    event_count: int = Field(default=0, description="Total number of events returned")
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
