# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and persisted state of the ready-edge schedule."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRuntimeReadySnapshot(_FrozenModel):
    """Two facts from the same container and the verdict of the runtime's readiness probe."""

    started_at: AwareDatetime
    built_at: AwareDatetime
    ready: bool

    @model_validator(mode="after")
    def _valid_build_time(self) -> "ModelRuntimeReadySnapshot":
        if self.built_at > self.started_at:
            raise ValueError("the runtime's build timestamp is newer than its start")
        return self


class ModelRunnerReadReadyEdge(_FrozenModel):
    """A ready boot and the window cut from the preceding observed build."""

    started_at: AwareDatetime
    previous_build_at: AwareDatetime
    ready_at: AwareDatetime
    read_started: bool = False
    alarm_raised: bool = False


class ModelRunnerReadReadyState(_FrozenModel):
    """The last successful Temporal schedule result, passed into the next tick."""

    state_schema: Literal["ready-edge-v1"] = "ready-edge-v1"
    latest_boot: ModelRuntimeReadySnapshot | None = None
    previous_build_at: AwareDatetime | None = None
    pending: tuple[ModelRunnerReadReadyEdge, ...] = ()


class ModelRunnerReadReadyRequest(_FrozenModel):
    state: ModelRunnerReadReadyState | None
    snapshot: ModelRuntimeReadySnapshot | None
    now: AwareDatetime


class ModelRunnerReadReadySettings(_FrozenModel):
    probe_interval_seconds: int = Field(gt=0)
    probe_timeout_seconds: int = Field(gt=0)
    tick_timeout_seconds: int = Field(gt=0)
    idle_bound_seconds: int = Field(gt=0)
    ready_path: str = Field(pattern=r"^/[A-Za-z0-9_./-]*$")
    host_reads_overlay_id: str = Field(min_length=1)
    host_reads_file_env: str = Field(min_length=1)
    alarm_state_relpath: str = Field(min_length=1)
