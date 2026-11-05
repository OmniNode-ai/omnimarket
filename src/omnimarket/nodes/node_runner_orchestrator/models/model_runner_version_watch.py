# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed runner watch policy, deployment facts and observed results."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Version = Annotated[str, StringConstraints(pattern=r"^v?\d+\.\d+\.\d+$")]


class ModelWatchFrozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelListenerProbe(ModelWatchFrozen):
    runner_name: NonEmpty
    # An argv, executed without a shell; read Runner.Listener itself, not an image label.
    version_command: tuple[NonEmpty, ...] = Field(min_length=1)


class ModelWatchPool(ModelWatchFrozen):
    pool_id: NonEmpty
    fleet_state_path: NonEmpty
    listeners: tuple[ModelListenerProbe, ...] = Field(min_length=1)


class ModelRunnerWatchOverlay(ModelWatchFrozen):
    pin_path: NonEmpty
    exception_state_dir: NonEmpty
    releases_url: NonEmpty
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    pools: tuple[ModelWatchPool, ...] = Field(min_length=1)


class ModelRunnerWatchPolicy(ModelWatchFrozen):
    max_releases_behind: int = Field(ge=0)
    observation_max_age_seconds: int = Field(gt=0)
    max_future_skew_seconds: int = Field(ge=0)


class ModelRunnerWatchTopics(ModelWatchFrozen):
    command: NonEmpty
    completed: NonEmpty
    alarm: NonEmpty
    attention: NonEmpty


class ModelRunnerWatchRequest(ModelWatchFrozen):
    correlation_id: UUID
    now: AwareDatetime
    overlay: ModelRunnerWatchOverlay | None = None


class ModelWatchPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    runner_version: Version


class ModelWatchRelease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    tag_name: NonEmpty
    draft: bool
    prerelease: bool


class ModelWatchObservedRunner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    runner_name: NonEmpty
    status: Literal["online", "busy", "offline", "unknown"]


class ModelWatchFleetState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    observed_at: AwareDatetime
    runners: tuple[ModelWatchObservedRunner, ...]


class ModelRunnerWatchFinding(ModelWatchFrozen):
    code: Literal[
        "offline",
        "off_pin",
        "behind_latest",
        "pin_behind_latest",
        "listener_unreadable",
        "fleet_state_unreadable",
        "fleet_state_stale",
        "pin_unreadable",
        "release_source_unreadable",
        "configuration_unreadable",
    ]
    pool_id: str | None = None
    runner_name: str | None = None
    listener_version: str | None = None
    releases_behind: int | None = None
    # Error classes only: subprocess output or URLs may contain deployment secrets.
    error_type: str | None = None


class ModelRunnerWatchResult(ModelWatchFrozen):
    correlation_id: UUID
    observed_at: AwareDatetime
    status: Literal["healthy", "alarm", "error"]
    pin_version: str | None = None
    latest_version: str | None = None
    pools_checked: tuple[str, ...] = ()
    findings: tuple[ModelRunnerWatchFinding, ...] = ()
