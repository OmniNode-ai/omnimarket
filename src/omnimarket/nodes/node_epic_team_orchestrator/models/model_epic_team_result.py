# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_epic_team_orchestrator [OMN-12206].

ModelEpicTeamResult: aggregated outcome emitted after all waves complete and
the DoD compliance gate runs.

Supporting models:
- ModelWaveResult: per-wave summary (dispatched, merged, failed, stalled counts)
- ModelTicketOutcome: per-ticket disposition record
- ModelStallEvent: stall record for an individual worker
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTicketDisposition(StrEnum):
    """Terminal disposition for a ticket within the epic run."""

    MERGED = "merged"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALLED = "stalled"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class EnumEpicTeamRunStatus(StrEnum):
    """Overall run status for the epic team orchestration."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class EnumDodGateStatus(StrEnum):
    """Result of the post-orchestration DoD compliance gate."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class ModelTicketOutcome(BaseModel):
    """Per-ticket disposition record within an epic team run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket ID (e.g. 'OMN-2001').")
    repo: str = Field(default="", description="Target repo name (e.g. 'omniclaude').")
    disposition: EnumTicketDisposition = Field(
        description="Terminal disposition for this ticket.",
    )
    pr_url: str = Field(
        default="",
        description="GitHub PR URL if a PR was created; empty string otherwise.",
    )
    branch: str = Field(
        default="",
        description="Git branch used for this ticket; empty string if not created.",
    )
    wave_id: int = Field(
        default=0,
        description="Wave index in which this ticket was dispatched (0-based).",
    )
    failure_class: str = Field(
        default="",
        description=(
            "Failure class string (e.g. 'ci_failure_ruff', 'stale_branch') "
            "when disposition is FAILED or BLOCKED; empty string otherwise."
        ),
    )
    retry_count: int = Field(
        default=0,
        description="Number of times this ticket was retried within the current run.",
    )


class ModelStallEvent(BaseModel):
    """Record of a stall detection event for a single worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket ID of the stalled worker.")
    wave_id: int = Field(description="Wave index in which the stall was detected.")
    idle_seconds: int = Field(
        description="Seconds the worker was idle before stall was declared.",
    )
    retry_wave: int = Field(
        description="Wave index in which the ticket will be retried (or -1 if blocked).",
    )


class ModelWaveResult(BaseModel):
    """Per-wave summary after all workers in a wave have reported or been timed out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    wave_id: int = Field(description="Zero-based wave index.")
    dispatched_count: int = Field(
        default=0,
        description="Number of tickets dispatched in this wave.",
    )
    merged_count: int = Field(
        default=0,
        description="Number of tickets that completed with MERGED disposition.",
    )
    failed_count: int = Field(
        default=0,
        description="Number of tickets that completed with FAILED disposition.",
    )
    stalled_count: int = Field(
        default=0,
        description="Number of tickets that were stalled and queued for retry.",
    )
    skipped_count: int = Field(
        default=0,
        description="Number of tickets skipped due to failed verification or pattern gate.",
    )


class ModelEpicTeamResult(BaseModel):
    """Output of the epic team orchestrator after all waves and the DoD gate complete."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str = Field(description="Linear epic ID that was orchestrated.")
    run_status: EnumEpicTeamRunStatus = Field(
        description="Overall run status.",
    )
    wave_results: tuple[ModelWaveResult, ...] = Field(
        default=(),
        description="Ordered per-wave summaries (wave_id=0 first).",
    )
    completed_tickets: tuple[ModelTicketOutcome, ...] = Field(
        default=(),
        description="Tickets that reached a terminal MERGED or SKIPPED disposition.",
    )
    failed_tickets: tuple[ModelTicketOutcome, ...] = Field(
        default=(),
        description="Tickets that reached a terminal FAILED, BLOCKED, or TIMEOUT disposition.",
    )
    stall_events: tuple[ModelStallEvent, ...] = Field(
        default=(),
        description="All stall detection events recorded during the run.",
    )
    dod_gate_status: EnumDodGateStatus = Field(
        default=EnumDodGateStatus.SKIPPED,
        description="Result of the post-orchestration DoD compliance gate.",
    )
    total_tickets: int = Field(
        default=0,
        description="Total number of child tickets processed across all waves.",
    )
    dry_run: bool = Field(
        default=False,
        description="True when the run was executed in dry-run mode.",
    )
