# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed input event for skill-executions projection snapshots."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelSkillExecutionProjectionEvent(BaseModel):
    """Skill-lifecycle event fields consumed by the skill-executions reducer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    skill_name: str = Field(
        default="unknown",
        validation_alias=AliasChoices("skill_name", "skillName", "skill"),
    )
    repo_id: str = Field(
        default="unknown",
        validation_alias=AliasChoices("repo_id", "repoId", "repo", "repository"),
    )
    event_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("event_type", "eventType"),
    )
    status: str | None = Field(default=None)
    window: str = Field(default="latest")
    emitted_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices("emitted_at", "emittedAt", "timestamp"),
    )


__all__ = ["ModelSkillExecutionProjectionEvent"]
