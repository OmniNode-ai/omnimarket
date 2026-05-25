# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Subtask state models for the swarm subtask state reducer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field


class EnumSubtaskState(StrEnum):
    ASSIGNED = "assigned"
    EXECUTING = "executing"
    ESCALATING = "escalating"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"


TERMINAL_STATES: frozenset[EnumSubtaskState] = frozenset(
    {
        EnumSubtaskState.COMPLETED,
        EnumSubtaskState.FAILED,
        EnumSubtaskState.TIMED_OUT,
        EnumSubtaskState.BLOCKED,
    }
)


class ModelSubtaskState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    subtask_id: str
    state: EnumSubtaskState
    endpoint_id: str = ""
    model_id: str = ""
    assigned_at: str = ""
    completed_at: str = ""
    latency_ms: int = 0
    attempt_count: int = 0
    failure_class: str = ""
    terminal_event_id: str = ""


class ModelSwarmRunState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    subtasks: dict[str, ModelSubtaskState]
    total_count: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed_count(self) -> int:
        return sum(
            1 for s in self.subtasks.values() if s.state == EnumSubtaskState.COMPLETED
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_count(self) -> int:
        return sum(
            1
            for s in self.subtasks.values()
            if s.state in (EnumSubtaskState.FAILED, EnumSubtaskState.TIMED_OUT)
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending_count(self) -> int:
        return sum(1 for s in self.subtasks.values() if s.state not in TERMINAL_STATES)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocked_count(self) -> int:
        return sum(
            1 for s in self.subtasks.values() if s.state == EnumSubtaskState.BLOCKED
        )


__all__: list[str] = [
    "TERMINAL_STATES",
    "EnumSubtaskState",
    "ModelSubtaskState",
    "ModelSwarmRunState",
]
