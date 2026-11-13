# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the schedule run reducer (OMN-19998)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import ValidationError

from omnimarket.handlers.schedule_run_store import (
    InMemoryScheduleRunStore,
    get_schedule_run_store,
    reset_schedule_run_store,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def _event(
    job_id: str,
    fired_at: datetime,
    *,
    outcome: Literal["completed", "failed"] = "completed",
    detail: str = "",
    scheduled_for: datetime | None = None,
) -> ModelScheduleRunEvent:
    return ModelScheduleRunEvent(
        job_id=job_id,
        node_name=f"node_{job_id}",
        tick_id=f"tick-{job_id}",
        scheduled_for=scheduled_for or fired_at,
        fired_at=fired_at,
        outcome=outcome,
        detail=detail,
    )


@pytest.fixture(autouse=True)
def _fresh_store() -> Iterator[None]:
    reset_schedule_run_store()
    yield
    reset_schedule_run_store()


def test_golden_the_first_run_is_held_with_fire_count_one() -> None:
    applied = HandlerScheduleRunReducer().handle(_event("job_a", T0))
    assert applied.applied is True
    assert applied.fire_count == 1
    held = get_schedule_run_store().get("job_a")
    assert held is not None
    assert held.fire_count == 1


def test_golden_a_newer_run_folds_to_fire_count_two() -> None:
    h = HandlerScheduleRunReducer()
    h.handle(_event("job_a", T0))
    newer = T0 + timedelta(minutes=1)
    applied = h.handle(_event("job_a", newer))
    assert applied.applied is True
    assert applied.fire_count == 2


def test_golden_two_jobs_are_held_apart_and_state_lists_them_sorted() -> None:
    h = HandlerScheduleRunReducer()
    h.handle(_event("job_b", T0))
    h.handle(_event("job_a", T0))
    states = get_schedule_run_store().state()
    assert [s.job_id for s in states] == ["job_a", "job_b"]


def test_error_an_older_run_is_refused_and_the_held_state_stays() -> None:
    h = HandlerScheduleRunReducer()
    h.handle(_event("job_a", T0 + timedelta(minutes=1)))
    refused = h.handle(_event("job_a", T0))
    assert refused.applied is False
    assert "not newer" in refused.reason
    held = get_schedule_run_store().get("job_a")
    assert held is not None
    assert held.fire_count == 1


def test_error_a_redelivery_of_the_same_run_converges() -> None:
    store = InMemoryScheduleRunStore()
    h = HandlerScheduleRunReducer(store)
    assert h.handle(_event("job_a", T0)).applied is True
    assert h.handle(_event("job_a", T0)).applied is False
    held = store.get("job_a")
    assert held is not None
    assert held.fire_count == 1


def test_error_a_failed_run_increments_failure_count_and_keeps_the_detail() -> None:
    h = HandlerScheduleRunReducer()
    h.handle(_event("job_a", T0))
    failed = h.handle(
        _event("job_a", T0 + timedelta(minutes=1), outcome="failed", detail="boom")
    )
    assert failed.applied is True
    held = get_schedule_run_store().get("job_a")
    assert held is not None
    assert held.failure_count == 1
    assert held.last_failure_detail == "boom"


def test_error_a_later_completed_run_keeps_the_failure_count() -> None:
    h = HandlerScheduleRunReducer()
    h.handle(_event("job_a", T0, outcome="failed", detail="boom"))
    h.handle(_event("job_a", T0 + timedelta(minutes=1)))
    held = get_schedule_run_store().get("job_a")
    assert held is not None
    assert held.failure_count == 1
    assert held.last_outcome == "completed"


def test_error_an_unknown_outcome_string_fails_model_validation() -> None:
    with pytest.raises(ValidationError):
        ModelScheduleRunEvent.model_validate(
            {
                **_event("job_a", T0).model_dump(),
                "outcome": "exploded",
            }
        )
