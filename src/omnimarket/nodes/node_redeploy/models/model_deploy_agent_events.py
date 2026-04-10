# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the deploy agent's Kafka event schema.

Topics:
  Publish: onex.cmd.deploy.rebuild-requested.v1
  Subscribe: onex.evt.deploy.rebuild-completed.v1

Schema mirrors the deploy agent sidecar design:
  docs/plans/2026-04-03-deploy-agent-sidecar-design.md
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumRedeployScope(StrEnum):
    """Scope of a runtime rebuild command."""

    FULL = "full"
    RUNTIME = "runtime"
    CORE = "core"


class EnumPhaseResult(StrEnum):
    """Per-phase result in rebuild-completed event."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    PENDING = "pending"


class EnumRedeployStatus(StrEnum):
    """Top-level rebuild status."""

    SUCCESS = "success"
    FAILED = "failed"


class ModelHealthCheck(BaseModel):
    """Single service health check result."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    service: str = Field(..., description="Service name.")
    endpoint: str = Field(..., description="Endpoint checked.")
    status: Literal["pass", "fail"] = Field(..., description="Check result.")
    latency_ms: int = Field(default=0, description="Latency in milliseconds.")


class ModelDeployRebuildCommand(BaseModel):
    """Command to trigger a deploy agent rebuild.

    Published to: onex.cmd.deploy.rebuild-requested.v1
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="UUID correlation ID for tracking.")
    requested_by: str = Field(
        default="node_redeploy",
        description="Identity of the requester.",
    )
    scope: EnumRedeployScope = Field(
        default=EnumRedeployScope.FULL,
        description="Scope of rebuild.",
    )
    services: list[str] = Field(
        default_factory=list,
        description="Optional service filter. Empty = scope default.",
    )
    git_ref: str = Field(
        default="origin/main",
        description="Git ref to deploy.",
    )


class ModelDeployPhaseResults(BaseModel):
    """Phase-level results from the deploy agent."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    git: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    core: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    runtime: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    verification: EnumPhaseResult = Field(default=EnumPhaseResult.SKIPPED)
    publish: EnumPhaseResult = Field(default=EnumPhaseResult.PENDING)


class ModelDeployRebuildCompleted(BaseModel):
    """Completion event from the deploy agent.

    Received from: onex.evt.deploy.rebuild-completed.v1
    """

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    correlation_id: str = Field(
        ..., description="Must match the command correlation_id."
    )
    requested_git_ref: str = Field(default="", description="Echo of git_ref input.")
    git_sha: str = Field(default="", description="Git SHA after pull.")
    started_at: datetime | None = Field(default=None, description="Rebuild start time.")
    completed_at: datetime | None = Field(default=None, description="Rebuild end time.")
    duration_seconds: float = Field(default=0.0, description="Total duration.")
    scope: str = Field(default="full", description="Scope that was rebuilt.")
    services_restarted: list[str] = Field(
        default_factory=list,
        description="Services that were restarted.",
    )
    phase_results: ModelDeployPhaseResults = Field(
        default_factory=ModelDeployPhaseResults,
        description="Per-phase outcomes.",
    )
    status: EnumRedeployStatus = Field(
        ...,
        description="success | failed",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-phase error messages. Empty on success.",
    )
    health_checks: list[ModelHealthCheck] = Field(
        default_factory=list,
        description="Service health check results.",
    )


class ModelRedeployResult(BaseModel):
    """Structured result returned by HandlerRedeployKafka.execute()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="The correlation ID used.")
    success: bool = Field(..., description="True if deploy agent reported success.")
    status: EnumRedeployStatus = Field(..., description="success | failed")
    duration_seconds: float = Field(default=0.0, description="Total wall-clock time.")
    git_sha: str = Field(default="", description="Git SHA deployed.")
    services_restarted: list[str] = Field(
        default_factory=list,
        description="Services restarted by the deploy agent.",
    )
    phase_results: dict[str, str] = Field(
        default_factory=dict,
        description="Phase name -> result string.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Errors from deploy agent.",
    )
    timed_out: bool = Field(
        default=False,
        description="True if polling timed out before completion event arrived.",
    )


__all__: list[str] = [
    "EnumPhaseResult",
    "EnumRedeployScope",
    "EnumRedeployStatus",
    "ModelDeployPhaseResults",
    "ModelDeployRebuildCommand",
    "ModelDeployRebuildCompleted",
    "ModelHealthCheck",
    "ModelRedeployResult",
]
