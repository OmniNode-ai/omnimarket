# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run configuration the lab-fill planner reads, supplied by the caller from its overlay (OMN-20668).

The planner holds no project, operator, repository or host identity: the project
and operator ids, the pull-request repositories and the area exclusions all arrive
in this model, which the caller builds from its deployment overlay.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TICKET_RE = re.compile(r"OMN-[0-9]+")
_RUN_KEY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4}Z)?")


def _words(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in values if v.strip())


class ModelLabFillHeadroomPolicy(BaseModel):
    """How many lanes a host can carry, from its real idle and free memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_busy_per_core: float = Field(default=0.75, gt=0, le=1)
    lane_cores: float = Field(default=2, gt=0)
    lane_mem_gb: float = Field(default=8, gt=0)
    mem_headroom_gb: float = Field(default=2, gt=0)
    max_per_host_per_run: int = Field(default=4, ge=1, le=12)


class ModelLabFillPlanConfig(BaseModel):
    """Scope and caps of one lab-fill run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_ticket: str
    milestone: str
    run_key: str
    project_id: str = ""
    operator_id: str = ""
    pr_repos: tuple[str, ...] = ()
    exclude_areas: tuple[str, ...] = ()
    defect_labels: tuple[str, ...] = ()
    defect_terms: tuple[str, ...] = ()
    max_lanes: int = Field(default=8, ge=1, le=12)
    fallback_max_age_min: int = Field(default=720, ge=1, le=10080)

    @field_validator("parent_ticket")
    @classmethod
    def _ticket(cls, value: str) -> str:
        value = value.strip()
        if _TICKET_RE.fullmatch(value) is None:
            raise ValueError(f"parent_ticket must be OMN-<n>, got '{value}'")
        return value

    @field_validator("milestone")
    @classmethod
    def _milestone(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("milestone must not be blank")
        return value

    @field_validator("run_key")
    @classmethod
    def _run_key(cls, value: str) -> str:
        if _RUN_KEY_RE.fullmatch(value) is None:
            raise ValueError(f"run_key must be YYYY-MM-DD[THHMMZ], got '{value}'")
        return value

    @field_validator(
        "pr_repos", "exclude_areas", "defect_labels", "defect_terms", mode="after"
    )
    @classmethod
    def _lowered(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _words(value)

    @model_validator(mode="after")
    def _operator_with_project(self) -> Self:
        if self.project_id and not self.operator_id:
            raise ValueError("operator_id is required with project_id")
        return self
