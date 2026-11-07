# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, source read and result of the runner-group exposure probe."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_runner_group_exposure_compute.models.model_runner_group_exposure import (
    EnumExposureReadFailure,
    ModelGroupJob,
    ModelGroupWorkflowFile,
    ModelRunnerGroupExposureReading,
    ModelRunnerGroupFacts,
)


class ModelExposureProbeRequest(BaseModel):
    """One probe: the correlation id and the time the read is judged at (no clock is read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime


class ModelGroupRead(BaseModel):
    """What a source read of one group returned, or why it could not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: ModelRunnerGroupFacts | None = None
    files: tuple[ModelGroupWorkflowFile, ...] = ()
    jobs: tuple[ModelGroupJob, ...] = ()
    read_at: datetime
    calls: int = Field(ge=0)
    read_failure: EnumExposureReadFailure | None = None


class ModelExposureProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    backend: Literal["inmemory", "github"]
    calls: int = Field(ge=0)
    reading: ModelRunnerGroupExposureReading


__all__ = ["ModelExposureProbeRequest", "ModelExposureProbeResult", "ModelGroupRead"]
