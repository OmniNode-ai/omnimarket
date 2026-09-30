# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-recorded execution evidence; the node never executes targets."""

from pydantic import BaseModel, ConfigDict, Field


class ModelRubricExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: str = Field(min_length=1)
    passed: bool
