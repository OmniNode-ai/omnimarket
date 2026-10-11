# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request of node_github_schedule_observer_effect (OMN-20803).

One hourly tick. Which repositories, clones and paths to watch are deployment
facts, so the request carries them; the node holds none.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
)

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr, ProcessId

RepositorySlug = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
]


class ModelObservedRepository(BaseModel):
    """One repository the observer watches, and what makes a merge to it runtime-affecting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositorySlug
    runtime_paths: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="Path prefixes whose change makes a merge runtime-affecting. "
        "Empty: the repository's merges are not reconciled.",
    )


class ModelExternalDeadmanSource(BaseModel):
    """The external dead-man's workflow, whose last completed run is read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId = Field(description="Its overlay entry.")
    repository: RepositorySlug
    workflow_file: NonEmptyStr = Field(description="File name under .github/workflows.")


class ModelObserverIdentity(BaseModel):
    """The observer's own identity, for its heartbeat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr


class ModelGithubScheduleObserverRequest(BaseModel):
    """One tick of the GitHub schedule observer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: AwareDatetime
    overlay_path: NonEmptyStr | None = Field(
        default=None,
        description="The liveness overlay file; absent reads the neutral default.",
    )
    state_path: NonEmptyStr = Field(description="Where the cursors persist.")
    clone_root: NonEmptyStr = Field(
        description="Directory holding the clones, each named by its repository name."
    )
    repositories: tuple[ModelObservedRepository, ...]
    published_trigger_keys: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="Merge commit shas whose trigger the bus already carried.",
    )
    external_deadman: ModelExternalDeadmanSource | None = None
    observer: ModelObserverIdentity | None = None
    initial_lookback_seconds: PositiveInt = 86400
    workflow_state_interval_seconds: PositiveInt = 86400
    max_pages: PositiveInt = 10
