# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory schedule run store, the default profile of the schedule run reducer."""

from __future__ import annotations

import threading

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleJobState,
    ModelScheduleRunApplied,
    ModelScheduleRunEvent,
)
from omnimarket.protocols.protocol_schedule_run_store import (
    ProtocolScheduleRunStore,
)


class InMemoryScheduleRunStore:
    """Holds the last run and the counts of each scheduled job; a redelivered or older run is refused."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, ModelScheduleJobState] = {}

    def apply(self, event: ModelScheduleRunEvent) -> ModelScheduleRunApplied:
        with self._lock:
            held = self._held.get(event.job_id)
            if held is not None and event.fired_at <= held.last_fired_at:
                return ModelScheduleRunApplied(
                    applied=False,
                    job_id=event.job_id,
                    fire_count=held.fire_count,
                    reason="refused: not newer than the held run",
                )
            failed = event.outcome == "failed"
            fire_count = held.fire_count + 1 if held is not None else 1
            failure_count = (held.failure_count if held is not None else 0) + (
                1 if failed else 0
            )
            last_failure_detail = (
                event.detail
                if failed
                else (held.last_failure_detail if held is not None else "")
            )
            self._held[event.job_id] = ModelScheduleJobState(
                job_id=event.job_id,
                last_fired_at=event.fired_at,
                last_scheduled_for=event.scheduled_for,
                last_outcome=event.outcome,
                fire_count=fire_count,
                failure_count=failure_count,
                last_failure_detail=last_failure_detail,
            )
            return ModelScheduleRunApplied(
                applied=True,
                job_id=event.job_id,
                fire_count=fire_count,
                reason="held as the latest run",
            )

    def state(self) -> tuple[ModelScheduleJobState, ...]:
        with self._lock:
            return tuple(self._held[job_id] for job_id in sorted(self._held))

    def get(self, job_id: str) -> ModelScheduleJobState | None:
        with self._lock:
            return self._held.get(job_id)


_STORE: ProtocolScheduleRunStore = InMemoryScheduleRunStore()


def get_schedule_run_store() -> ProtocolScheduleRunStore:
    return _STORE


def bind_schedule_run_store(store: ProtocolScheduleRunStore) -> None:
    global _STORE
    _STORE = store


def reset_schedule_run_store() -> None:
    bind_schedule_run_store(InMemoryScheduleRunStore())
