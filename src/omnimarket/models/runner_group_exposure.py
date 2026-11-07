# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and reading of the runner-group exposure read.

The request carries facts a fetching effect already read (the org group settings, the
workflow files that select the group's labels, the jobs queued or running on the group) and
the settings from the overlay (which events count as public-origin, how old a read may be).
The reading is one verdict per subject and one overall verdict. Nothing here reads a clock
or a network.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumExposureReadFailure(StrEnum):
    """Why the fetching effect could not read the group; every one reads UNKNOWN."""

    ORG_READ_DENIED = "org-read-denied"
    BUDGET_EXHAUSTED = "budget-exhausted"
    UNREADABLE = "unreadable"


class ModelRunnerGroupFacts(BaseModel):
    """The org's settings of one runner group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: int
    allows_public_repositories: bool
    restricted_to_workflows: bool
    selected_workflows: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()


class ModelGroupWorkflowFile(BaseModel):
    """One workflow file of a group repository, as parsed on its default branch.

    ``selects_group_label`` is true when the file selects a label of the group directly, through a
    repository variable that resolves to one, or through a ``uses:`` chain into a reusable workflow.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    path: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    selects_group_label: bool
    triggers: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        """The form the group's selected_workflows list uses: repository/path@ref."""
        return f"{self.repository}/{self.path}@{self.ref}"


class ModelGroupJob(BaseModel):
    """One job queued or in progress on a runner whose labels intersect the group's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    run_event: str = Field(min_length=1)
    head_is_fork: bool = False
    workflow_ref: str = Field(min_length=1)


class ModelExposureSettings(BaseModel):
    """The overlay's settings; no default here is a policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_reach_events: tuple[str, ...] = Field(min_length=1)
    max_age_seconds: int = Field(gt=0)


class ModelRunnerGroupExposureRequest(BaseModel):
    """The facts of one read, the settings, and the time it is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    read_at: datetime
    settings: ModelExposureSettings
    group: ModelRunnerGroupFacts | None = None
    files: tuple[ModelGroupWorkflowFile, ...] = ()
    jobs: tuple[ModelGroupJob, ...] = ()
    read_failure: EnumExposureReadFailure | None = None


class ModelExposureSubject(BaseModel):
    """One subject of the reading: its verdict, its count and the reason in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumReadingVerdict
    count: int | None = None
    reason: str


class ModelRunnerGroupExposureReading(BaseModel):
    """The reading: one overall verdict and the subjects behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdict: EnumReadingVerdict
    subjects: tuple[ModelExposureSubject, ...]
    read_failure: EnumExposureReadFailure | None = None


__all__: list[str] = [
    "EnumExposureReadFailure",
    "ModelExposureSettings",
    "ModelExposureSubject",
    "ModelGroupJob",
    "ModelGroupWorkflowFile",
    "ModelRunnerGroupExposureReading",
    "ModelRunnerGroupExposureRequest",
    "ModelRunnerGroupFacts",
]
