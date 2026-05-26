# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Check name")
    passed: bool = Field(description="Whether this check passed")
    detail: str = Field(
        default="", description="Human-readable detail or violation message"
    )


class ModelPlanAuditComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | error")
    passed: bool = Field(default=False, description="True when all checks pass")
    checks: list[ModelCheckResult] = Field(
        default_factory=list, description="Per-check result objects"
    )
    violations: list[str] = Field(
        default_factory=list, description="Human-readable violation messages"
    )
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
