# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persisted cursors of node_github_schedule_observer_effect (OMN-20803)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationRunPhase,
    ProcessId,
)


class ModelEmittedRun(BaseModel):
    """The last phase published for a run still inside the re-read window."""

    model_config = ConfigDict(extra="forbid")

    phase: EnumAutomationRunPhase
    created_at: AwareDatetime


class ModelOutstandingRun(BaseModel):
    """A run first seen unfinished; read again until it completes."""

    model_config = ConfigDict(extra="forbid")

    process_id: ProcessId
    github_run_id: int = Field(ge=1)
    created_at: AwareDatetime


class ModelRepositoryObserverState(BaseModel):
    """Cursors of one repository."""

    model_config = ConfigDict(extra="forbid")

    runs_cursor: AwareDatetime | None = Field(
        default=None, description="Newest run creation time read completely."
    )
    emitted: dict[str, ModelEmittedRun] = Field(default_factory=dict)
    outstanding: dict[str, ModelOutstandingRun] = Field(default_factory=dict)
    workflow_states_read_at: AwareDatetime | None = None
    closed_pr_cursor: AwareDatetime | None = Field(
        default=None, description="Newest merge time reconciled completely."
    )
    examined_merges: dict[str, AwareDatetime] = Field(
        default_factory=dict,
        description="Merge commit sha -> merge time, for merges already reconciled.",
    )


class ModelScheduleObserverState(BaseModel):
    """Everything the observer persists between ticks."""

    model_config = ConfigDict(extra="forbid")

    first_tick_at: AwareDatetime | None = None
    ticks_completed: int = Field(default=0, ge=0)
    repositories: dict[str, ModelRepositoryObserverState] = Field(default_factory=dict)
    external_deadman_last_run_id: str | None = None
