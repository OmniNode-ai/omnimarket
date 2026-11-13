# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, working memory and terminal event of the branch deploy orchestrator (OMN-24027)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.host_install_state import (
    EnumHostInstallState,
    ModelHostInstallRecord,
    ModelHostInstallStepEvent,
)


class EnumBranchDeployPhase(StrEnum):
    """The step whose result the orchestrator is waiting for."""

    RECORDING = "recording"
    COMMAND = "command"
    JOB = "job"
    PROBE = "probe"
    ROLLBACK_COMMAND = "rollback_command"
    ROLLBACK_JOB = "rollback_job"
    ROLLBACK_PROBE = "rollback_probe"
    # A finished deploy's row: it stays stored and ignores every later result.
    DONE = "done"


class EnumBranchDeployNext(StrEnum):
    """What the orchestrator does when the reducer's record for the step it published comes back."""

    SNAPSHOT = "snapshot"
    INSTALL_START = "install_start"
    SEND_DEPLOY = "send_deploy"
    SEND_PROBE = "send_probe"
    AFTER_VERIFY = "after_verify"
    SEND_ROLLBACK = "send_rollback"
    FINISH = "finish"


class ModelBranchDeployRequest(BaseModel):
    """One deploy of one open PR's head to the dev runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    runtime_lane: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    pr_number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    rollback_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    rollback_image: str = Field(min_length=1)
    rollback_reason: str = Field(min_length=1)


class ModelDeployJobCompleted(BaseModel):
    """Mirrors the deploy agent's rebuild-completed event."""

    model_config = ConfigDict(extra="ignore")

    correlation_id: UUID
    git_sha: str
    runtime_lane: str
    status: Literal["success", "failed"]
    phase_results: dict[str, str]
    errors: list[str] = []


class ModelDeployJobRejected(BaseModel):
    """Mirrors the deploy agent's rebuild-rejected event."""

    model_config = ConfigDict(extra="ignore")

    correlation_id: UUID
    reason: str = Field(min_length=1)


class ModelBranchDeployCompleted(BaseModel):
    """The terminal event: how the deploy ended, with the digests the reducer holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    unit: str
    final_state: EnumHostInstallState
    outcome: Literal["deployed", "rolled_back", "failed"]
    head_sha: str
    observed_sha: str = ""
    rollback_failures: int = 0
    detail: str = ""


class ModelBranchDeployContext(BaseModel):
    """The stored row of one deploy: the request, the phase awaited, and what follows the next record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelBranchDeployRequest
    phase: EnumBranchDeployPhase
    prior: ModelHostInstallRecord | None = None
    pending_steps: tuple[ModelHostInstallStepEvent, ...] = ()
    next_action: EnumBranchDeployNext = EnumBranchDeployNext.SNAPSHOT
    observed_sha: str = ""


__all__: list[str] = [
    "EnumBranchDeployNext",
    "EnumBranchDeployPhase",
    "ModelBranchDeployCompleted",
    "ModelBranchDeployContext",
    "ModelBranchDeployRequest",
    "ModelDeployJobCompleted",
    "ModelDeployJobRejected",
]
