# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed DeclaredTool contract."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_tool_parameter import (
    ModelToolParameter,
)


class ModelDeclaredTool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    parameters: tuple[ModelToolParameter, ...]

    @model_validator(mode="after")
    def unique_parameters(self) -> Self:
        names = [row.name for row in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("duplicate parameter names")
        return self
