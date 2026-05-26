# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSkillVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Skill name")
    status: str = Field(description="Verdict: ok | stub | gap | error")
    stubs_found: list[str] = Field(
        default_factory=list, description="Handler paths that raise NotImplementedError"
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Missing contracts, broken wiring, or unreachable paths",
    )


class ModelSkillFunctionalAuditComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | error")
    verdicts: list[ModelSkillVerdict] = Field(
        default_factory=list, description="Per-skill audit verdicts"
    )
    stubs_found: list[str] = Field(
        default_factory=list,
        description="Skill names whose handlers raise NotImplementedError",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Skill names with missing contracts or broken wiring",
    )
    total_audited: int = Field(default=0, description="Total number of skills audited")
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
