# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the schedule run reducer holds the last run and the counts of each scheduled job."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleJobState,
    ModelScheduleRunApplied,
    ModelScheduleRunEvent,
)


@runtime_checkable
class ProtocolScheduleRunStore(Protocol):
    """Schedule run store: the reducer folds run events into it, the gate reads it."""

    def apply(self, event: ModelScheduleRunEvent) -> ModelScheduleRunApplied:
        """Fold the event into the held state; refuse a redelivered or older run."""
        ...

    def state(self) -> tuple[ModelScheduleJobState, ...]:
        """The held state of every job, sorted by job_id."""
        ...

    def get(self, job_id: str) -> ModelScheduleJobState | None:
        """The held state of one job, or None when it has never run."""
        ...
