# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the runner-group reading (OMN-22947).

The request carries facts a fetching effect already read (the organization's runner groups with
their runners, the repositories, the canary public workflow's runs) and the settings from the
overlay. The result is one verdict per subject and one overall verdict. Nothing here reads a clock
or a network.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumGroupReadFailure(StrEnum):
    """Why the fetching effect could not read the groups; every one reads UNKNOWN."""

    ORG_READ_DENIED = "org-read-denied"
    BUDGET_EXHAUSTED = "budget-exhausted"
    UNREADABLE = "unreadable"


class EnumGroupVisibility(StrEnum):
    """The organization API's visibility of a runner group."""

    ALL = "all"
    SELECTED = "selected"
    PRIVATE = "private"


class EnumRepositoryVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    INTERNAL = "internal"


class ModelGroupRunner(BaseModel):
    """One runner of a group, by name, labels and whether the organization lists it online.

    ``online`` is None when the read did not carry the runner's status; that reads UNKNOWN for a subject that needs it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    online: bool | None = None


class ModelGroupFact(BaseModel):
    """The organization's setting of one runner group and the runners it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: int
    name: str = Field(min_length=1)
    visibility: EnumGroupVisibility
    allows_public_repositories: bool
    selected_repositories: tuple[str, ...] = ()
    runners: tuple[ModelGroupRunner, ...]


class ModelRepositoryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    visibility: EnumRepositoryVisibility


class ModelCanaryRun(BaseModel):
    """One run of the canary public workflow and the runner group that picked it up, if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    repository: str = Field(min_length=1)
    picked_up_by_group_id: int | None = None


class ModelCanaryFacts(BaseModel):
    """The canary public workflow's runs in the read window. A canary that was not read is None."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    runs: tuple[ModelCanaryRun, ...] = ()


class ModelMergeGroupJobFact(BaseModel):
    """One completed merge-group job read from a run record: its repository, its result and the runner that ran it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    repository: str = Field(min_length=1)
    repository_visibility: EnumRepositoryVisibility
    conclusion: str = Field(min_length=1)
    runner_name: str | None = None
    runner_group_id: int | None = None


class ModelPrivateQueueSettings(BaseModel):
    """The overlay's private merge-queue pool: the group's name, its runners' name prefix and how many must be online."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_name: str = Field(min_length=1)
    runner_name_prefix: str = Field(min_length=1)
    expected_online_members: int = Field(gt=0)


class ModelRunnerGroupReadingSettings(BaseModel):
    """The overlay's settings; no default here is a policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lab_runner_labels: tuple[str, ...] = Field(min_length=1)
    max_age_seconds: int = Field(gt=0)
    private_queue: ModelPrivateQueueSettings | None = None


class ModelRunnerGroupReadingRequest(BaseModel):
    """The facts of one read, the settings, and the time it is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    read_at: datetime
    settings: ModelRunnerGroupReadingSettings
    groups: tuple[ModelGroupFact, ...] = ()
    repositories: tuple[ModelRepositoryFact, ...] = ()
    canary: ModelCanaryFacts | None = None
    merge_group_jobs: tuple[ModelMergeGroupJobFact, ...] | None = None
    read_failure: EnumGroupReadFailure | None = None


class ModelReadingSubject(BaseModel):
    """One subject of the reading: its verdict, its count and the reason in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumReadingVerdict
    count: int | None = None
    reason: str


class ModelRunnerGroupReadingResult(BaseModel):
    """The reading: one overall verdict and the subjects behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdict: EnumReadingVerdict
    subjects: tuple[ModelReadingSubject, ...]
    groups_read: int
    failing_group_ids: tuple[int, ...] = ()
    read_failure: EnumGroupReadFailure | None = None
