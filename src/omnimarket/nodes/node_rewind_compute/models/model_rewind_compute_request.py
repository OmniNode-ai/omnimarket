# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRewindComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str = Field(description="Name of the agent whose event stream to query")
    timestamp: str = Field(
        description="ISO 8601 anchor timestamp for the rewind window"
    )
    window_seconds: int = Field(
        default=3600,
        description="Look-back window in seconds from the anchor timestamp",
    )
