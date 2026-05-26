# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_dispatch_watchdog_orchestrator [OMN-12209].

Contains:
- ModelWaveTask: a single dispatched Task() in the current wave.
- ModelStallEvent: a stall detection record for a single task.
- ModelRecoveryAction: recovery action taken (report / cancel / redispatch).
- ModelWatchdogSummary: aggregate stats across a wave check.
- ModelWatchdogResult: output from the watchdog orchestrator.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRecoveryAction(StrEnum):
    """Recovery action the watchdog may take on a stalled task."""

    REPORT = "report"
    CANCEL = "cancel"
    REDISPATCH = "redispatch"


class EnumTaskStatus(StrEnum):
    """Observed lifecycle status of a Task()."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ModelWaveTask(BaseModel):
    """A single Task() dispatch record within the monitored wave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Internal Task() identifier.")
    ticket_id: str = Field(
        description="Linear ticket ID this task is working (e.g. OMN-1234)."
    )
    team_name: str = Field(
        description="Team name under which the Task() was dispatched."
    )
    status: EnumTaskStatus = Field(
        default=EnumTaskStatus.IN_PROGRESS,
        description="Last known lifecycle status.",
    )
    last_tool_name: str | None = Field(
        default=None,
        description="Name of the last tool call observed (e.g. 'Bash', 'Read').",
    )
    last_tool_timeout_ms: int | None = Field(
        default=None,
        description="Timeout parameter of the last Bash call in milliseconds, if applicable.",
    )
    last_activity_ts: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp of the last observed tool call.",
    )
    redispatch_count: int = Field(
        default=0,
        ge=0,
        description="Number of times this task has been redispatched by the watchdog.",
    )


class ModelStallEvent(BaseModel):
    """A stall detection record for a single task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Task() identifier that stalled.")
    ticket_id: str = Field(
        description="Linear ticket ID associated with the stalled task."
    )
    last_activity_ts: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp of last known activity.",
    )
    idle_seconds: float = Field(
        ge=0.0,
        description="Elapsed seconds since last tool call at detection time.",
    )
    bash_timeout_exemption: bool = Field(
        default=False,
        description="True if the stall threshold was extended due to a long-running Bash call.",
    )
    effective_timeout_seconds: float = Field(
        ge=0.0,
        description="Effective stall threshold applied (may exceed check_interval if Bash exemption active).",
    )
    action_taken: EnumRecoveryAction = Field(
        description="Recovery action applied to this stall.",
    )
    redispatch_attempt: int = Field(
        ge=0,
        description="Redispatch attempt number (0 = first redispatch).",
    )
    recovery_task_id: str | None = Field(
        default=None,
        description="New Task() ID if redispatched; None for report/cancel.",
    )


class ModelRecoveryAction(BaseModel):
    """A single recovery action record emitted by the watchdog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        description="Linear ticket ID on which the action was taken."
    )
    task_id: str = Field(description="Stalled Task() identifier.")
    action: EnumRecoveryAction = Field(description="Type of recovery action taken.")
    escalated_to_blocked: bool = Field(
        default=False,
        description="True if the ticket was moved to Blocked in Linear due to max_redispatches exceeded.",
    )
    friction_event_path: str | None = Field(
        default=None,
        description="Path to the friction event file written on escalation.",
    )


class ModelWatchdogSummary(BaseModel):
    """Aggregate statistics across one watchdog check pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_tasks: int = Field(ge=0, description="Total tasks observed in the wave.")
    healthy: int = Field(ge=0, description="Tasks showing active progress.")
    stalled: int = Field(ge=0, description="Tasks declared stalled this check.")
    blocked: int = Field(
        ge=0, description="Tasks escalated to Blocked (max redispatches exceeded)."
    )
    redispatched: int = Field(ge=0, description="Tasks redispatched this check.")
    cancelled: int = Field(ge=0, description="Tasks cancelled this check.")


class ModelWatchdogResult(BaseModel):
    """Output from the dispatch watchdog orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str | None = Field(
        default=None,
        description="Epic ID being monitored, if provided.",
    )
    check_timestamp_utc: str = Field(
        description="ISO-8601 UTC timestamp when this check was executed.",
    )
    healthy_task_ids: tuple[str, ...] = Field(
        default=(),
        description="Task IDs that are actively progressing.",
    )
    stall_events: tuple[ModelStallEvent, ...] = Field(
        default=(),
        description="Stall detection records for all stalled tasks.",
    )
    recovery_actions: tuple[ModelRecoveryAction, ...] = Field(
        default=(),
        description="Recovery actions taken this pass.",
    )
    summary: ModelWatchdogSummary = Field(
        description="Aggregate stats for this check pass.",
    )
    watchdog_log_path: str | None = Field(
        default=None,
        description="Path to the watchdog.json state file written for this check.",
    )
    dispatch_log_path: str | None = Field(
        default=None,
        description="Path to the NDJSON dispatch-log appended by this check.",
    )
