# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_self_healing_dispatch_orchestrator [OMN-12208].

ModelSelfHealingDispatchResult: aggregated outcome emitted after all workers
complete (or are escalated) and the orchestration loop exits.

Supporting models:
- ModelDispatchGroup: per-repo grouping of tickets dispatched together
- ModelWorkerRecord: record for a single dispatched worker
- ModelStallRecoveryEvent: record of a stall detection and recovery action
- ModelEscalationRecord: record of a ticket escalated to Blocked
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumWorkerStatus(StrEnum):
    """Terminal status for a dispatched worker."""

    COMPLETED = "completed"
    STALLED = "stalled"
    ESCALATED = "escalated"
    FAILED = "failed"


class EnumDispatchRunStatus(StrEnum):
    """Overall run status for the self-healing dispatch orchestration."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class ModelDispatchGroup(BaseModel):
    """Per-repo grouping of tickets that were dispatched as a single worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Target repo name (e.g. 'omniclaude').")
    ticket_ids: tuple[str, ...] = Field(
        description="Ordered tuple of ticket IDs dispatched to this repo.",
    )
    worker_name: str = Field(
        default="",
        description="TeamCreate worker name assigned to this group.",
    )


class ModelWorkerRecord(BaseModel):
    """Record for a single dispatched worker within an orchestration run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_name: str = Field(description="TeamCreate worker name.")
    repo: str = Field(description="Target repo name.")
    ticket_ids: tuple[str, ...] = Field(
        description="Ticket IDs handled by this worker.",
    )
    status: EnumWorkerStatus = Field(
        description="Terminal status of this worker.",
    )
    redispatch_attempt: int = Field(
        default=0,
        description=(
            "Redispatch attempt index (0 = initial dispatch, 1 = first recovery, etc.)."
        ),
    )


class ModelStallRecoveryEvent(BaseModel):
    """Record of a stall detection and recovery action for a single ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket ID of the stalled worker.")
    repo: str = Field(description="Repo the stalled ticket was assigned to.")
    redispatch_attempt: int = Field(
        description="Attempt count at the time of stall (1-based).",
    )
    max_redispatches: int = Field(
        description="Maximum redispatches allowed for this run.",
    )
    recovery_worker_name: str = Field(
        default="",
        description="Name of the recovery worker launched for this stall (empty if escalated).",
    )
    escalated: bool = Field(
        default=False,
        description="True when the ticket exceeded max_redispatches and was escalated to Blocked.",
    )


class ModelEscalationRecord(BaseModel):
    """Record of a ticket escalated to Blocked after exhausting redispatch attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket ID that was escalated.")
    repo: str = Field(description="Repo the ticket was assigned to.")
    attempt_count: int = Field(
        description="Total dispatch attempts made before escalation.",
    )


class ModelSelfHealingDispatchResult(BaseModel):
    """Output of the self-healing dispatch orchestrator after the run completes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Correlation ID for this orchestration run.")
    run_status: EnumDispatchRunStatus = Field(
        description="Overall run status.",
    )
    dispatch_groups: tuple[ModelDispatchGroup, ...] = Field(
        default=(),
        description="Ordered repo-grouped dispatch plans (one entry per repo).",
    )
    dispatched_workers: tuple[ModelWorkerRecord, ...] = Field(
        default=(),
        description="All workers dispatched during the run (initial + recovery).",
    )
    stall_events: tuple[ModelStallRecoveryEvent, ...] = Field(
        default=(),
        description="All stall detection and recovery events recorded during the run.",
    )
    escalated_tickets: tuple[ModelEscalationRecord, ...] = Field(
        default=(),
        description="Tickets that were escalated to Blocked after exhausting redispatches.",
    )
    total_tickets: int = Field(
        default=0,
        description="Total number of tickets processed in this run.",
    )
    stalls_recovered: int = Field(
        default=0,
        description="Number of stalls that were successfully recovered (not escalated).",
    )
    elapsed_seconds: int = Field(
        default=0,
        description="Wall-clock seconds from run start to completion.",
    )
    dry_run: bool = Field(
        default=False,
        description="True when the run was executed in dry-run mode.",
    )
    log_path: str = Field(
        default="",
        description="Absolute path to the NDJSON dispatch log for this run.",
    )
