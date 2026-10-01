# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ToolCallResult contract."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_tool_call_status import (
    EnumToolCallStatus,
)


class ModelToolCallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumToolCallStatus
    output: str
