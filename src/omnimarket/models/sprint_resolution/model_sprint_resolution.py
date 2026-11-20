# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and answer of the current-sprint resolver.

A sprint is a Linear project named ``Sprint <start> → <end>`` whose own start and target dates
hold the date asked about. Its milestone and due date come from the same project, so a ticket is
placed in a sprint with all three resolved together and none typed by hand.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class EnumSprintResolutionStatus(StrEnum):
    """What the resolver found for a date."""

    RESOLVED = "resolved"
    NO_SPRINT = "no_sprint"
    AMBIGUOUS = "ambiguous"


class ModelSprintMilestone(BaseModel):
    """One milestone of a listed project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = ""
    target_date: str | None = None


class ModelSprintProjectListing(BaseModel):
    """One Linear project as the caller listed it, with its milestones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str
    start_date: str | None = None
    target_date: str | None = None
    completed_at: str | None = None
    milestones: tuple[ModelSprintMilestone, ...] = ()


class ModelSprintResolveRequest(BaseModel):
    """The date to place and the projects the caller listed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: str = Field(
        description="One UTC day, YYYY-MM-DD, taken from the clock by the caller."
    )
    projects: tuple[ModelSprintProjectListing, ...] = ()

    @field_validator("date")
    @classmethod
    def _is_day(cls, value: str) -> str:
        if not _DAY_RE.match(value):
            raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
        return value


class ModelSprintResolution(BaseModel):
    """The sprint a date falls in, or the typed reason it falls in none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumSprintResolutionStatus
    date: str
    project_id: str = ""
    project_name: str = ""
    milestone_id: str = ""
    milestone_name: str = ""
    due_date: str = Field(default="", description="The project's target date.")
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumSprintResolutionStatus.RESOLVED


__all__ = [
    "EnumSprintResolutionStatus",
    "ModelSprintMilestone",
    "ModelSprintProjectListing",
    "ModelSprintResolution",
    "ModelSprintResolveRequest",
]
