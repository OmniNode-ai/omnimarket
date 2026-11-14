# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24177 AC3 (scheduling half): two tracking runs of one record run the same tasks, and a changed record is a new record."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from omnimarket.models.eval_tracking.model_eval_tracking_record import (
    ModelEvalTrackingRecord,
)
from omnimarket.nodes.node_eval_tracking_schedule_compute.handlers import (
    HandlerEvalTrackingSchedule,
)
from tests.nodes.node_eval_tracking_schedule_compute.support import (
    DAY,
    EPOCH,
    LOCAL,
    candidates,
    make_overlay,
    record_document,
    tick_at,
    turns,
)

pytestmark = pytest.mark.unit


def test_two_runs_of_one_record_carry_the_same_tasks_and_the_digest_they_were_drawn_with() -> (
    None
):
    overlay = make_overlay()
    handler = HandlerEvalTrackingSchedule(overlay)
    first = handler.handle(tick_at(next(turns(LOCAL, EPOCH, EPOCH + DAY))))
    second = handler.handle(
        tick_at(next(turns(LOCAL, EPOCH + 7 * DAY, EPOCH + 8 * DAY)))
    )
    assert first is not None
    assert second is not None
    assert first.correlation_id != second.correlation_id
    assert first.candidates == second.candidates == overlay.record.candidates
    assert overlay.record.task_sample_digest == make_overlay().record.task_sample_digest


def test_a_run_request_carries_one_arm_and_publishes_no_cells() -> None:
    handler = HandlerEvalTrackingSchedule(make_overlay())
    request = handler.handle(tick_at(next(turns(LOCAL, EPOCH, EPOCH + DAY))))
    assert request is not None
    assert [(a.model, a.backend_id) for a in request.arms] == [("local-a", "lab-a")]
    assert request.publish_cells is False
    assert request.tenant_id == "lab"
    assert request.harness_revision == "rev-1"


def test_a_changed_record_is_a_new_record_and_names_new_runs() -> None:
    at = tick_at(next(turns(LOCAL, EPOCH, EPOCH + DAY)))
    old = HandlerEvalTrackingSchedule(make_overlay()).handle(at)
    for change in ({"seed": "seed-2"}, {"prompt_template": "template-v2"}):
        new_record = record_document(**change)
        new = HandlerEvalTrackingSchedule(make_overlay(record=new_record)).handle(at)
        assert old is not None
        assert new is not None
        assert new.correlation_id != old.correlation_id, change


def test_the_record_refuses_a_digest_that_does_not_match_its_tasks() -> None:
    other = candidates(3)
    with pytest.raises(ValidationError, match="task_sample_digest"):
        ModelEvalTrackingRecord.model_validate(record_document(candidates=other))


def test_the_record_digest_does_not_depend_on_task_order() -> None:
    sample = candidates(4)
    reordered = ModelEvalTrackingRecord.model_validate(
        record_document(candidates=tuple(reversed(sample)))
    )
    assert reordered.task_sample_digest == make_overlay().record.task_sample_digest


def test_the_record_refuses_a_model_in_two_arms_a_repeated_task_and_a_naive_epoch() -> (
    None
):
    arm = {"model": "m", "backend_id": "b", "cadence": "local"}
    with pytest.raises(ValidationError, match="two arms"):
        ModelEvalTrackingRecord.model_validate(record_document(arms=[arm, arm]))
    twice = candidates(2) + candidates(1)
    from omnimarket.models.eval_tracking.model_eval_tracking_record import sample_digest

    with pytest.raises(ValidationError, match="twice"):
        ModelEvalTrackingRecord.model_validate(
            record_document(candidates=twice, task_sample_digest=sample_digest(twice))
        )
    with pytest.raises(ValidationError, match="UTC"):
        ModelEvalTrackingRecord.model_validate(
            record_document(epoch=dt.datetime(2026, 11, 16))
        )
