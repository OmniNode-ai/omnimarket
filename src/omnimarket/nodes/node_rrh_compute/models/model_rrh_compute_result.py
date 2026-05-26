# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRrhCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Check name")
    passed: bool = Field(description="Whether this check passed")
    detail: str = Field(default="", description="Detail or failure reason")


class ModelRrhComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | error")
    ready: bool = Field(default=False, description="True when all required checks pass")
    results: list[ModelRrhCheckResult] = Field(
        default_factory=list, description="Per-check validation results"
    )
    blocking_checks: list[str] = Field(
        default_factory=list,
        description="Names of checks that failed and block the release",
    )
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
