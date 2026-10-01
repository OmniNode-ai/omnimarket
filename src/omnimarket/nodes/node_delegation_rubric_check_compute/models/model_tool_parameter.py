# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ToolParameter contract."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_tool_parameter_type import (
    EnumToolParameterType,
)


class ModelToolParameter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    json_type: EnumToolParameterType
    required: bool
