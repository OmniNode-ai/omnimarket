# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The summary of one poll, published as the tick's terminal event."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    NonEmptyStr,
    ProcessId,
)


class ModelObserverFinding(BaseModel):
    """A verdict the observer itself reached while reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    verdict: EnumAutomationLivenessVerdict
    reason: EnumAutomationLivenessReason
    detail: str = ""


class ModelAutomationRunObserverResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: NonEmptyStr
    observed_at: AwareDatetime
    entries_read: NonNegativeInt = Field(
        description="Declared observer entries on this host that were read."
    )
    runs_emitted: NonNegativeInt = Field(
        description="Run events newly journaled by this poll."
    )
    unseen_runs: NonNegativeInt = Field(
        description="Runs that happened between two reads of latest-only evidence."
    )
    findings: tuple[ModelObserverFinding, ...] = ()
    events_journaled: NonNegativeInt = Field(
        description="Events of this poll appended to the on-disk journal."
    )
    events_published: NonNegativeInt = Field(
        description="Journaled events (this poll's and older) the broker accepted."
    )
    journal_backlog: NonNegativeInt = Field(
        description="Events still on disk waiting for the broker."
    )
    journal_corrupt_lines: NonNegativeInt = 0
    flush_error: str | None = Field(
        default=None, description="Why the flush stopped; the backlog stays on disk."
    )
