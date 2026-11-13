# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the schedule verdict compute (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
    ModelScheduleJobState,
    ModelScheduleVerdictRequest,
)
from omnimarket.nodes.node_schedule_verdict_compute.handlers.handler_schedule_verdict import (
    HandlerScheduleVerdict,
)

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def _decl(
    job_id: str = "job_a", run_interval_seconds: int = 60, grace_seconds: int = 0
) -> ModelScheduleDeclaration:
    return ModelScheduleDeclaration(
        job_id=job_id,
        run_interval_seconds=run_interval_seconds,
        grace_seconds=grace_seconds,
    )


def _state(
    job_id: str,
    last_fired_at: datetime,
    last_outcome: Literal["completed", "failed"] = "completed",
) -> ModelScheduleJobState:
    return ModelScheduleJobState(
        job_id=job_id,
        last_fired_at=last_fired_at,
        last_scheduled_for=last_fired_at,
        last_outcome=last_outcome,
        fire_count=1,
        failure_count=0 if last_outcome == "completed" else 1,
        last_failure_detail="" if last_outcome == "completed" else "boom",
    )


def test_golden_a_job_fired_inside_its_interval_is_firing_and_not_red() -> None:
    now = T0 + timedelta(seconds=30)
    request = ModelScheduleVerdictRequest(
        now=now,
        observed_since=T0,
        declared=(_decl(),),
        states=(_state("job_a", T0),),
    )
    result = HandlerScheduleVerdict().handle(request)
    assert result.red is False
    assert result.verdicts[0].state == "FIRING"
    assert result.verdicts[0].age_seconds == 30


def test_golden_a_job_not_yet_due_since_observed_since_is_pending_and_not_red() -> None:
    now = T0 + timedelta(seconds=30)
    request = ModelScheduleVerdictRequest(
        now=now,
        observed_since=T0,
        declared=(_decl(),),
        states=(),
    )
    result = HandlerScheduleVerdict().handle(request)
    assert result.red is False
    assert result.verdicts[0].state == "PENDING"


def test_error_a_job_never_fired_past_interval_plus_grace_is_never_fired_and_red() -> (
    None
):
    now = T0 + timedelta(seconds=120)
    request = ModelScheduleVerdictRequest(
        now=now,
        observed_since=T0,
        declared=(_decl(run_interval_seconds=60, grace_seconds=30),),
        states=(),
    )
    result = HandlerScheduleVerdict().handle(request)
    assert result.red is True
    assert result.verdicts[0].state == "NEVER_FIRED"
    assert "job_a" in result.reason


def test_error_a_job_last_fired_longer_ago_than_interval_plus_grace_is_overdue_and_red() -> (
    None
):
    now = T0 + timedelta(seconds=120)
    request = ModelScheduleVerdictRequest(
        now=now,
        observed_since=T0,
        declared=(_decl(run_interval_seconds=60, grace_seconds=30),),
        states=(_state("job_a", T0),),
    )
    result = HandlerScheduleVerdict().handle(request)
    assert result.red is True
    assert result.verdicts[0].state == "OVERDUE"
    assert "job_a" in result.reason


def test_error_a_job_whose_last_run_failed_is_failing_and_red() -> None:
    now = T0 + timedelta(seconds=30)
    request = ModelScheduleVerdictRequest(
        now=now,
        observed_since=T0,
        declared=(_decl(),),
        states=(_state("job_a", T0, last_outcome="failed"),),
    )
    result = HandlerScheduleVerdict().handle(request)
    assert result.red is True
    assert result.verdicts[0].state == "FAILING"
    assert "job_a" in result.reason


def test_error_two_jobs_one_firing_one_overdue_gives_red_naming_the_overdue_one() -> (
    None
):
    now = T0 + timedelta(seconds=120)
    request = ModelScheduleVerdictRequest(
        now=now,
        observed_since=T0,
        declared=(_decl("job_a"), _decl("job_b")),
        states=(_state("job_a", T0 + timedelta(seconds=90)), _state("job_b", T0)),
    )
    result = HandlerScheduleVerdict().handle(request)
    assert result.red is True
    states = {v.job_id: v.state for v in result.verdicts}
    assert states["job_a"] == "FIRING"
    assert states["job_b"] == "OVERDUE"
    assert "job_b" in result.reason
    assert "job_a" not in result.reason
