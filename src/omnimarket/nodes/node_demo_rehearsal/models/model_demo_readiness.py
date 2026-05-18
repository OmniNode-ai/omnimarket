# SPDX-License-Identifier: MIT
"""Shared Pydantic models for the demo readiness team nodes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumDemoCriticality(StrEnum):
    """Criticality classification for demo drift findings."""

    DEMO_BLOCKER = "demo_blocker"
    DEMO_DEGRADED = "demo_degraded"
    COSMETIC = "cosmetic"
    OBSERVABILITY_ONLY = "observability_only"
    BACKLOG_ONLY = "backlog_only"


class EnumDemoRehearsalStatus(StrEnum):
    """Overall status of a demo rehearsal run."""

    GREEN = "GREEN"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"


class ModelRehearsalBundle(BaseModel):
    """Full evidence bundle produced by a single demo rehearsal run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rehearsal_id: str = Field(..., description="Unique run ID for this rehearsal.")
    timestamp_utc: datetime = Field(
        ..., description="UTC timestamp when rehearsal ran."
    )
    command_envelope: dict[str, Any] = Field(
        default_factory=dict,
        description="The demo command envelope that was executed.",
    )
    runtime_topology_manifest: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime topology snapshot at rehearsal time.",
    )
    projection_row: dict[str, Any] | None = Field(
        default=None,
        description="Projection row from the database (None if unavailable).",
    )
    dashboard_api_response: dict[str, Any] | None = Field(
        default=None,
        description="Dashboard API response payload (None if unavailable).",
    )
    screenshot_path: str | None = Field(
        default=None,
        description="Relative path to screenshot artifact (None if not captured).",
    )
    overall_status: Literal["GREEN", "DEGRADED", "BROKEN"] = Field(
        ..., description="Overall rehearsal health."
    )
    failures: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of failure detail dicts.",
    )


class ModelDriftFinding(BaseModel):
    """A single drift finding from the drift detector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(..., description="Unique finding identifier.")
    dimension: str = Field(
        ...,
        description="Drift dimension: topology, projection, dashboard, screenshot.",
    )
    criticality: EnumDemoCriticality = Field(..., description="Criticality level.")
    summary: str = Field(..., description="One-line summary of the drift.")
    detail: str = Field(default="", description="Extended detail or diff.")
    auto_fixable: bool = Field(
        default=False,
        description="Whether this finding can be auto-fixed without human approval.",
    )
    fix_hint: str | None = Field(
        default=None,
        description="Suggested fix action for auto-fixable findings.",
    )


class ModelDispatchIssue(BaseModel):
    """A single issue item in the morning dispatch plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_id: str = Field(..., description="Issue identifier.")
    criticality: EnumDemoCriticality = Field(..., description="Criticality level.")
    summary: str = Field(..., description="One-line summary.")
    fix_dispatched: bool = Field(
        default=False,
        description="Whether an auto-fix was dispatched for this issue.",
    )
    pr_url: str | None = Field(
        default=None,
        description="Auto-fix PR URL if dispatched.",
    )
    requires_human: bool = Field(
        default=False,
        description="Whether human action is required.",
    )


class ModelMorningDispatchPlan(BaseModel):
    """Machine-readable morning dispatch plan produced by node_morning_handoff_generator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime = Field(
        ..., description="UTC timestamp when plan was generated."
    )
    overnight_summary: str = Field(
        ..., description="Human-readable overnight summary paragraph."
    )
    issues: list[ModelDispatchIssue] = Field(
        default_factory=list,
        description="All issues requiring attention.",
    )
    proposed_dispatch_waves: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Proposed parallel fix waves for the morning session.",
    )


class ModelBoundedConcurrencyConfig(BaseModel):
    """Concurrency and cost limits for the auto-fix dispatcher."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_parallel_workers: int = Field(default=4, ge=1, le=16)
    max_runtime_minutes: int = Field(default=120, ge=1, le=480)
    max_daily_cost_usd: float = Field(default=50.0, gt=0.0, le=500.0)
    max_open_autofix_prs: int = Field(default=5, ge=1, le=20)


__all__: list[str] = [
    "EnumDemoCriticality",
    "EnumDemoRehearsalStatus",
    "ModelBoundedConcurrencyConfig",
    "ModelDispatchIssue",
    "ModelDriftFinding",
    "ModelMorningDispatchPlan",
    "ModelRehearsalBundle",
]
