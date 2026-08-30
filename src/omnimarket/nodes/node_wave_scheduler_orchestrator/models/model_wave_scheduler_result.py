# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_wave_scheduler_orchestrator [OMN-12210, OMN-17017].

ModelWaveSchedulerResult: aggregated outcome emitted after all waves complete.

Supporting models:
- ModelWaveAssignment: per-wave ticket assignment record (execution order + DAG)
- ModelDependencyViolation: describes a dependency problem found during DAG validation
- ModelWaveExecutionSummary: per-wave execution outcome
- ModelObservedTicketOutcome: a durable, dispatcher-reported terminal outcome
- ModelDispatchLifecycleRecord: one append-only dispatch lifecycle transition
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumWaveSchedulerStatus(StrEnum):
    """Overall run status for the wave scheduler orchestration."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    ABORTED = "aborted"


class EnumTicketExecutionStatus(StrEnum):
    """Terminal execution status for a ticket within a wave run."""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALLED = "stalled"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    DEFERRED = "deferred"
    UNREPORTED = "unreported"
    """Dispatched, but the dispatcher returned no terminal status for it.

    OMN-17017: a missing status is a hard fact, never a silent ``"completed"``.
    An UNREPORTED ticket is not a completion — it stays visibly pending and its
    dependents are never dispatched.
    """


class EnumDispatchLifecyclePhase(StrEnum):
    """Durable lifecycle transitions recorded for a dispatched ticket.

    OMN-17017: "compiled" must never be a proxy for "executed". Each phase is a
    separate append-only record in the run's lifecycle log, so a ticket that was
    requested and never acknowledged is distinguishable from one that completed.
    """

    DISPATCH_REQUESTED = "dispatch_requested"
    OBSERVED = "observed"
    UNREPORTED = "unreported"


class EnumDependencyViolationKind(StrEnum):
    """Type of dependency violation detected during DAG validation."""

    CYCLE = "cycle"
    MISSING_DEPENDENCY = "missing_dependency"
    SELF_REFERENCE = "self_reference"


class ModelDependencyViolation(BaseModel):
    """Describes a dependency problem detected during DAG construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDependencyViolationKind = Field(
        description="Category of violation.",
    )
    ticket_id: str = Field(
        description="Ticket ID where the violation originates.",
    )
    dependency_id: str = Field(
        default="",
        description=(
            "Referenced dependency ID that caused the violation; "
            "empty for CYCLE violations where the cycle spans multiple nodes."
        ),
    )
    cycle_path: tuple[str, ...] = Field(
        default=(),
        description="Ordered ticket IDs forming the cycle (CYCLE violations only).",
    )
    message: str = Field(
        default="",
        description="Human-readable explanation of the violation.",
    )


class ModelWaveAssignment(BaseModel):
    """Per-wave ticket assignment record — the computed execution order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    wave_id: int = Field(description="Zero-based wave index.")
    ticket_ids: tuple[str, ...] = Field(
        default=(),
        description="Ordered list of ticket IDs assigned to this wave.",
    )
    repo_assignments: tuple[tuple[str, str], ...] = Field(
        default=(),
        description=(
            "Pairs of (ticket_id, repo) for tickets in this wave. "
            "Used for cross-repo lock deconfliction when defer_repo_conflicts=True."
        ),
    )
    deferred_ticket_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Tickets originally in this wave that were deferred to the next wave "
            "due to repo conflict deconfliction."
        ),
    )


class ModelWaveExecutionSummary(BaseModel):
    """Per-wave execution outcome after all workers report or time out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    wave_id: int = Field(description="Zero-based wave index.")
    dispatched_count: int = Field(
        default=0,
        description="Number of tickets dispatched in this wave.",
    )
    completed_count: int = Field(
        default=0,
        description="Tickets that completed successfully.",
    )
    failed_count: int = Field(
        default=0,
        description="Tickets that reached a FAILED terminal state.",
    )
    blocked_count: int = Field(
        default=0,
        description="Tickets blocked by failed upstream dependencies.",
    )
    stalled_count: int = Field(
        default=0,
        description="Tickets stalled and queued for retry.",
    )
    skipped_count: int = Field(
        default=0,
        description="Tickets skipped due to dry_run or gate failure.",
    )
    unreported_count: int = Field(
        default=0,
        description="Tickets dispatched for which the dispatcher reported no status.",
    )
    ticket_statuses: tuple[tuple[str, EnumTicketExecutionStatus], ...] = Field(
        default=(),
        description="Pairs of (ticket_id, status) for each ticket in this wave.",
    )


class ModelObservedTicketOutcome(BaseModel):
    """A dispatcher-reported terminal outcome read back from durable state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Ticket the outcome belongs to.")
    status: EnumTicketExecutionStatus = Field(
        description="Observed terminal execution status.",
    )
    reason: str = Field(
        default="",
        description="Optional terminal reason recorded by whatever executed the ticket.",
    )


class ModelDispatchLifecycleRecord(BaseModel):
    """One append-only dispatch lifecycle transition for a single ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Wave-scheduler run this record belongs to.")
    wave_id: int = Field(description="Zero-based wave index.")
    ticket_id: str = Field(description="Ticket the transition applies to.")
    repo: str = Field(
        default="",
        description="Repo the ticket was assigned to, when the plan declares one.",
    )
    phase: EnumDispatchLifecyclePhase = Field(
        description="Lifecycle transition recorded.",
    )
    status: EnumTicketExecutionStatus | None = Field(
        default=None,
        description="Observed status; set only on the OBSERVED phase.",
    )
    recorded_at_utc: str = Field(
        description="ISO-8601 UTC timestamp when the transition was recorded.",
    )


class ModelWaveSchedulerResult(BaseModel):
    """Output of the wave scheduler orchestrator after all waves complete."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str = Field(description="Path to the plan file that was executed.")
    run_status: EnumWaveSchedulerStatus = Field(
        description="Overall run status.",
    )
    wave_assignments: tuple[ModelWaveAssignment, ...] = Field(
        default=(),
        description=(
            "Computed wave schedule (populated even in dry_run mode). "
            "Ordered by wave_id (wave 0 first)."
        ),
    )
    wave_execution_summaries: tuple[ModelWaveExecutionSummary, ...] = Field(
        default=(),
        description=(
            "Per-wave execution outcomes. Empty in dry_run mode. Ordered by wave_id."
        ),
    )
    dependency_violations: tuple[ModelDependencyViolation, ...] = Field(
        default=(),
        description=(
            "DAG validation errors found during plan parsing. "
            "Non-empty implies run_status=FAILED and no dispatch was attempted."
        ),
    )
    total_tickets: int = Field(
        default=0,
        description="Total number of tickets in the plan.",
    )
    tickets_completed: int = Field(
        default=0,
        description="Number of tickets that completed successfully across all waves.",
    )
    tickets_failed: int = Field(
        default=0,
        description="Number of tickets that failed across all waves.",
    )
    tickets_blocked: int = Field(
        default=0,
        description="Number of tickets blocked by failed upstream dependencies.",
    )
    tickets_unreported: int = Field(
        default=0,
        description=(
            "Number of dispatched tickets for which the dispatcher reported no "
            "terminal status. Any non-zero value forbids run_status=COMPLETED."
        ),
    )
    tickets_skipped: int = Field(
        default=0,
        description="Number of tickets never dispatched because fail_fast aborted the run.",
    )
    dispatch_lifecycle_path: str | None = Field(
        default=None,
        description=(
            "Path to the append-only dispatch-lifecycle NDJSON written for this "
            "run, when the dispatcher exposes one."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="True when the run was executed in dry-run mode.",
    )
    resumed: bool = Field(
        default=False,
        description=(
            "True only when a checkpoint was actually read from "
            "<state_dir>/wave_scheduler/<run_id>/checkpoint.json. A resume "
            "request with no checkpoint on disk reports False (OMN-17017)."
        ),
    )
