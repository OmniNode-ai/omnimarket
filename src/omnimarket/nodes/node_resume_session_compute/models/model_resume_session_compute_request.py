# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelResumeSessionComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Unique task identifier to resume")
    agent_id: str = Field(description="Agent identifier whose session state to load")
