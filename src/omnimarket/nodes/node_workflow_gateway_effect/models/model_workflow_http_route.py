# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The HTTP route model for the workflow gateway mount."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkflowHandlerBinding(BaseModel):
    """The handler class a route is wired to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    module: str = Field(..., min_length=1)


class ModelWorkflowHttpRoute(BaseModel):
    """One HTTP route the gateway mount serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["submit", "status", "receipt", "contracts"]
    method: Literal["GET", "POST"]
    path: str = Field(..., min_length=1)
    handler: ModelWorkflowHandlerBinding


__all__: list[str] = ["ModelWorkflowHandlerBinding", "ModelWorkflowHttpRoute"]
