# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_runner_orchestrator [OMN-12218].

Typed representation of per-runner health status, host-level metrics,
and the aggregated action result emitted as the terminal event payload.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRunnerStatus(StrEnum):
    """GitHub API runner status."""

    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class EnumRunnerActionStatus(StrEnum):
    """Top-level outcome of the orchestrated runner action."""

    SUCCESS = "success"
    FAILURE = "failure"
    DRY_RUN = "dry_run"
    NOT_IMPLEMENTED = "not_implemented"


class ModelRunnerHealth(BaseModel):
    """Health snapshot for a single self-hosted runner container."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Runner display name (e.g. 'omninode-runner-9').")
    status: EnumRunnerStatus = Field(description="GitHub API runner status.")
    runner_group: str | None = Field(
        default=None, description="Runner group name (must be 'omnibase-ci')."
    )
    runner_version: str | None = Field(
        default=None,
        description="GitHub Actions runner binary version from Docker image label.",
    )
    gh_version: str | None = Field(
        default=None, description="gh CLI version from Docker image label."
    )
    kubectl_version: str | None = Field(
        default=None, description="kubectl version from Docker image label."
    )
    uv_version: str | None = Field(
        default=None, description="uv version from Docker image label."
    )
    uptime: str | None = Field(
        default=None,
        description="Container uptime string from 'docker ps --format {{.Status}}'.",
    )
    alerts: list[str] = Field(
        default_factory=list,
        description=(
            "Per-runner alert strings (e.g. '[OFFLINE] runner offline for 12m', "
            "'[VERSION] running 2.323.0 -- latest is 2.331.0')."
        ),
    )


class ModelHostMetrics(BaseModel):
    """Host-level metrics from the CI host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disk_path: str = Field(
        default="/var/lib/docker",
        description="Filesystem path reported by df.",
    )
    disk_used_pct: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Disk usage percentage. Alert fires at >= 70%.",
    )
    disk_total: str | None = Field(
        default=None, description="Total disk size (human-readable, e.g. '1.8 TB')."
    )
    disk_used: str | None = Field(
        default=None, description="Used disk space (human-readable)."
    )
    build_cache_size: str | None = Field(
        default=None,
        description="Docker build cache size (e.g. '0 B'). None if unsupported.",
    )
    alerts: list[str] = Field(
        default_factory=list,
        description="Host-level alert strings (e.g. '[DISK] /var/lib/docker at 74%').",
    )


class ModelRunnerResult(BaseModel):
    """Output envelope emitted by the runner orchestrator."""

    model_config = ConfigDict(extra="forbid")

    action_status: EnumRunnerActionStatus = Field(
        description="Top-level outcome of the runner action.",
    )
    runners: list[ModelRunnerHealth] = Field(
        default_factory=list,
        description="Per-runner health snapshots (populated by status and post-deploy check).",
    )
    host_metrics: ModelHostMetrics | None = Field(
        default=None,
        description="Host-level metrics (populated for status action).",
    )
    actions_taken: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable log of actions taken (e.g. "
            "'SSH to 192.168.86.201 — deploy-runners.sh exited 0')."  # onex-allow-internal-ip
        ),
    )
    dry_run_summary: str | None = Field(
        default=None,
        description="Dry-run compose diff and version comparison (deploy/update --dry-run only).",
    )
    error: str | None = Field(
        default=None,
        description="Error message when action_status is FAILURE.",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Correlation ID propagated from the request.",
    )
