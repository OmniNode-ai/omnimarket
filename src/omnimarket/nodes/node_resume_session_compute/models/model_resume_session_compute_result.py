# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelResumeSessionComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | not_found | error")
    session_state: dict[str, object] = Field(
        default_factory=dict, description="Full projected session state snapshot"
    )
    phase: str = Field(default="", description="Current session phase")
    progress_pct: float = Field(
        default=0.0, description="Estimated completion percentage (0.0-1.0)"
    )
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
