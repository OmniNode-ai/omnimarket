"""ModelDispatchWorkerCommand — input spec for worker dispatch compilation."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")
_TICKET_PATTERN = re.compile(r"^[A-Z0-9_-]{1,64}$")


class EnumWorkerRole(StrEnum):
    watcher = "watcher"
    fixer = "fixer"
    designer = "designer"
    auditor = "auditor"
    synthesizer = "synthesizer"
    sweep = "sweep"
    ops = "ops"


class ModelDispatchWorkerCommand(BaseModel):
    """Input spec for compiling a worker dispatch into a role-templated prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        ..., description="Worker handle (lowercase, hyphens/underscores ok)"
    )
    team: str = Field(..., description="Team name for task scoping")
    role: EnumWorkerRole = Field(..., description="Worker role (7 values)")
    scope: str = Field(..., description="Goal description")
    targets: list[str] = Field(..., description="Tickets/PRs/paths this worker owns")
    collision_fences: list[str] = Field(
        default_factory=list,
        description="Auto-populated from TaskList if empty",
    )
    reports_to: str = Field(default="team-lead", description="Agent to report to")
    wall_clock_cap_min: int | None = Field(
        default=None,
        ge=5,
        le=480,
        description="Wall-clock cap in minutes [5, 480]",
    )
    model: str = Field(default="sonnet", description="Model for Agent() spawn")
    replace: bool = Field(
        default=False,
        description="Kill existing in_progress worker with same name and restart",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_PATTERN.match(v):
            raise ValueError(f"name must match ^[a-z0-9_-]{{1,64}}$ (got {v!r})")
        return v

    @field_validator("team")
    @classmethod
    def validate_team(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("team must be a non-empty string")
        return v.strip()

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("targets must contain at least one entry")
        return v

    @field_validator("reports_to")
    @classmethod
    def validate_reports_to(cls, v: str) -> str:
        normalized = v.strip()
        if not normalized:
            raise ValueError("reports_to must be a non-empty string")
        return normalized


__all__: list[str] = ["EnumWorkerRole", "ModelDispatchWorkerCommand"]
