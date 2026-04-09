"""Shared Pydantic models for baseline capture and compare nodes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BaselineProbeType(StrEnum):
    """Named probe types supported by the baseline system."""

    GITHUB_PRS = "github_prs"
    LINEAR_TICKETS = "linear_tickets"
    SYSTEM_HEALTH = "system_health"
    KAFKA_TOPICS = "kafka_topics"
    GIT_BRANCHES = "git_branches"
    DB_ROW_COUNTS = "db_row_counts"


# ---------------------------------------------------------------------------
# Per-probe snapshot models
# ---------------------------------------------------------------------------


class ModelGitHubPRSnapshot(BaseModel):
    """Snapshot of a single GitHub pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(..., description="PR number.")
    title: str = Field(..., description="PR title.")
    repo: str = Field(..., description="Repository name (org/repo).")
    state: str = Field(..., description="PR state: open, closed, merged.")
    labels: list[str] = Field(default_factory=list, description="Label names.")
    age_days: float = Field(..., description="Age of the PR in days.")
    ci_status: str | None = Field(
        default=None, description="CI check status: success, failure, pending, etc."
    )


class ModelLinearTicketSnapshot(BaseModel):
    """Snapshot of a single Linear ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier (e.g. OMN-1234).")
    title: str = Field(..., description="Ticket title.")
    state: str = Field(..., description="Workflow state name.")
    priority: int | None = Field(
        default=None, description="Priority (0=no priority, 1=urgent, 4=low)."
    )
    assignee: str | None = Field(default=None, description="Assignee display name.")
    updated_at: datetime = Field(..., description="Last updated timestamp.")


class ModelServiceHealthSnapshot(BaseModel):
    """Snapshot of a single service health probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str = Field(..., description="Service name or URL.")
    healthy: bool = Field(..., description="Whether the service is healthy.")
    latency_ms: float | None = Field(
        default=None, description="Round-trip latency in milliseconds."
    )
    error: str | None = Field(default=None, description="Error message if unhealthy.")


class ModelKafkaTopicSnapshot(BaseModel):
    """Snapshot of a single Kafka/Redpanda topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., description="Topic name.")
    partition_count: int = Field(..., description="Number of partitions.")
    latest_offset: int = Field(..., description="Latest offset across all partitions.")


class ModelGitBranchSnapshot(BaseModel):
    """Snapshot of a single git worktree branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repository name.")
    branch: str = Field(..., description="Branch name.")
    worktree_path: str = Field(..., description="Absolute path to worktree.")
    age_days: float = Field(..., description="Age of the branch in days.")


class ModelDbRowCountSnapshot(BaseModel):
    """Snapshot of row count for a single DB table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_name: str = Field(..., description="Table name.")
    row_count: int = Field(..., description="Row count at capture time.")


# ---------------------------------------------------------------------------
# Aggregate snapshot
# ---------------------------------------------------------------------------

ProbeSnapshotItem = (
    ModelGitHubPRSnapshot
    | ModelLinearTicketSnapshot
    | ModelServiceHealthSnapshot
    | ModelKafkaTopicSnapshot
    | ModelGitBranchSnapshot
    | ModelDbRowCountSnapshot
)


class ModelBaselineSnapshot(BaseModel):
    """Aggregate system state snapshot captured by the baseline node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_id: str = Field(..., description="Unique identifier for this baseline.")
    captured_at: datetime = Field(..., description="UTC timestamp of capture.")
    label: str | None = Field(
        default=None, description="Human-readable label for this baseline."
    )
    probes: dict[str, list[ProbeSnapshotItem]] = Field(
        default_factory=dict,
        description="Probe name -> list of snapshot items.",
    )


# ---------------------------------------------------------------------------
# Per-probe delta models
# ---------------------------------------------------------------------------


class ModelGitHubPRDelta(BaseModel):
    """Delta for GitHub PR probe between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opened: list[int] = Field(
        default_factory=list, description="PR numbers newly opened."
    )
    closed: list[int] = Field(
        default_factory=list, description="PR numbers closed (not merged)."
    )
    merged: list[int] = Field(default_factory=list, description="PR numbers merged.")
    track_changes: dict[int, str] = Field(
        default_factory=dict,
        description="PR number -> state change description.",
    )


class ModelLinearTicketDelta(BaseModel):
    """Delta for Linear ticket probe between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opened: list[str] = Field(
        default_factory=list, description="Ticket IDs newly opened."
    )
    closed_done: list[str] = Field(
        default_factory=list, description="Ticket IDs moved to Done/Cancelled."
    )
    state_changes: dict[str, str] = Field(
        default_factory=dict,
        description="Ticket ID -> 'old_state -> new_state'.",
    )


class ModelServiceHealthDelta(BaseModel):
    """Delta for system health probe between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recovered: list[str] = Field(
        default_factory=list,
        description="Services that went from unhealthy to healthy.",
    )
    degraded: list[str] = Field(
        default_factory=list,
        description="Services that went from healthy to unhealthy.",
    )
    new_failures: list[str] = Field(
        default_factory=list,
        description="Services that appeared as unhealthy in the current snapshot.",
    )


class ModelKafkaTopicDelta(BaseModel):
    """Delta for Kafka topic probe between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created: list[str] = Field(
        default_factory=list, description="Topics newly created."
    )
    deleted: list[str] = Field(
        default_factory=list, description="Topics that disappeared."
    )
    offset_advances: dict[str, int] = Field(
        default_factory=dict,
        description="Topic -> offset delta (positive = messages produced).",
    )


class ModelGitBranchDelta(BaseModel):
    """Delta for git branch probe between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged: list[str] = Field(
        default_factory=list,
        description="Branches that disappeared (assumed merged).",
    )
    created: list[str] = Field(
        default_factory=list, description="Branches newly appearing."
    )
    stale: list[str] = Field(
        default_factory=list,
        description="Branches whose age exceeded a staleness threshold.",
    )


class ModelDbRowCountDelta(BaseModel):
    """Delta for DB row count probe between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grown: list[str] = Field(
        default_factory=list, description="Tables whose row count increased."
    )
    shrunk: list[str] = Field(
        default_factory=list, description="Tables whose row count decreased."
    )
    unchanged: list[str] = Field(
        default_factory=list, description="Tables with no row count change."
    )
    row_delta_by_table: dict[str, int] = Field(
        default_factory=dict,
        description="Table name -> row count delta (positive = grown).",
    )


# ---------------------------------------------------------------------------
# Aggregate delta
# ---------------------------------------------------------------------------

ProbeDeltaItem = (
    ModelGitHubPRDelta
    | ModelLinearTicketDelta
    | ModelServiceHealthDelta
    | ModelKafkaTopicDelta
    | ModelGitBranchDelta
    | ModelDbRowCountDelta
)


class ModelBaselineDelta(BaseModel):
    """Aggregated delta between a baseline snapshot and current state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_id: str = Field(..., description="Baseline ID being compared against.")
    baseline_captured_at: datetime = Field(
        ..., description="UTC timestamp of the original baseline."
    )
    compared_at: datetime = Field(
        ..., description="UTC timestamp when the comparison was run."
    )
    per_probe_deltas: dict[str, ProbeDeltaItem] = Field(
        default_factory=dict,
        description="Probe name -> delta model.",
    )


__all__: list[str] = [
    "BaselineProbeType",
    "ModelBaselineDelta",
    "ModelBaselineSnapshot",
    "ModelDbRowCountDelta",
    "ModelDbRowCountSnapshot",
    "ModelGitBranchDelta",
    "ModelGitBranchSnapshot",
    "ModelGitHubPRDelta",
    "ModelGitHubPRSnapshot",
    "ModelKafkaTopicDelta",
    "ModelKafkaTopicSnapshot",
    "ModelLinearTicketDelta",
    "ModelLinearTicketSnapshot",
    "ModelServiceHealthDelta",
    "ModelServiceHealthSnapshot",
    "ProbeDeltaItem",
    "ProbeSnapshotItem",
]
