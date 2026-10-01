# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ToolCall contract."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_tool_call_result import (
    ModelToolCallResult,
)


class ModelToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments_json: str
    result: ModelToolCallResult | None = None
