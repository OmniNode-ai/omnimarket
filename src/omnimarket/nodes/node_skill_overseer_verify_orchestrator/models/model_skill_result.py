# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill result model — output from the overseer_verify skill dispatch node."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SkillResultStatus(StrEnum):
    """Possible outcomes of a skill invocation."""

    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class ModelSkillResult(BaseModel):
    """Output from the overseer_verify skill dispatch node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str = Field(
        ...,
        min_length=1,
        description="Human-readable skill identifier matching the request",
    )
    status: SkillResultStatus = Field(
        ...,
        description="Final status of the skill invocation",
    )
    output: str | None = Field(
        default=None,
        description="Raw output text from the skill",
    )
    error: str | None = Field(
        default=None,
        description="Error detail when status is FAILED or PARTIAL",
    )


__all__ = ["ModelSkillResult", "SkillResultStatus"]
