# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSwarmDecomposeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    planner_output: str = Field(..., min_length=1)
    planner_model_id: str = Field(..., min_length=1)
    planner_output_hash: str = Field(..., min_length=1)
    endpoint_ids: tuple[str, ...] = Field(default=())
    original_task: str = Field(default="")
    correlation_id: str = Field(default="")
    decompose: bool = Field(default=True)
    token_threshold: int = Field(default=2000)
    context_window_limit: int = Field(default=0)
    max_subtasks: int = Field(default=6)
