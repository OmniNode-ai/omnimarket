# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: malformed requests are refused and an unread fact is never a zero (OMN-20676)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
    HandlerMergeSweepPlan,
    HandlerMergeSweepRetry,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepPlanRequest,
    ModelMergeSweepPlanResult,
    ModelMergeSweepRetryRequest,
)


def _plan(reading: dict[str, object], **extra: object) -> ModelMergeSweepPlanResult:
    request = ModelMergeSweepPlanRequest.model_validate({"reading": reading, **extra})
    return HandlerMergeSweepPlan().handle(request)


def test_a_request_without_a_reading_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelMergeSweepPlanRequest.model_validate({})


def test_an_unknown_request_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelMergeSweepRetryRequest.model_validate({"exit_code": 75, "wait": 5})


def test_a_red_without_a_repository_is_refused() -> None:
    with pytest.raises(ValidationError):
        _plan({"reds": [{"number": 3}]})


def test_an_unread_controller_and_product_plan_one_diagnose_lane() -> None:
    """Absent facts are not a healthy fleet: the sweep diagnoses why they are absent."""
    plan = _plan({})
    assert [lane.kind for lane in plan.lanes] == ["diagnose"]
    assert plan.lanes[0].reasons == [
        "controller: controller unread",
        "under floor: unread",
    ]
    assert plan.lab_only is True
    assert plan.load_per_core is None


def test_an_empty_controller_object_reads_as_unread() -> None:
    plan = _plan({"controller": {}, "product": {}})
    assert plan.lanes[0].reasons == [
        "controller: controller unread",
        "under floor: unread",
    ]


def test_a_healthy_fleet_plans_no_lane() -> None:
    plan = _plan(
        {
            "controller": {"stalled": False},
            "product": {"under_floor": []},
            "load1": 1.0,
            "cpus": 8,
        }
    )
    assert plan.lanes == []
    assert plan.dispatch == []
    assert plan.deferred == 0
    assert plan.lab_only is False


def test_load_never_removes_a_lane() -> None:
    reading = {
        "controller": {"stalled": False},
        "product": {"under_floor": []},
        "reds": [{"repo": "omnimarket", "number": 9, "state": "OPEN"}],
    }
    cool = _plan({**reading, "load1": 1.0, "cpus": 24})
    hot = _plan({**reading, "load1": 500.0, "cpus": 24})
    assert (
        [lane.kind for lane in cool.lanes]
        == [lane.kind for lane in hot.lanes]
        == ["fix"]
    )
    assert (cool.lab_only, hot.lab_only) == (False, True)


def test_a_repository_outside_the_scope_is_skipped_with_its_reason() -> None:
    plan = _plan(
        {
            "controller": {"stalled": False},
            "product": {"under_floor": []},
            "reds": [{"repo": "omniweb", "number": 1, "state": "OPEN"}],
        }
    )
    assert plan.lanes == []
    assert [(s.pr, s.why) for s in plan.skipped] == [("omniweb#1", "out-of-scope-repo")]


def test_a_red_chain_head_leads_its_repositorys_fix_lane() -> None:
    plan = _plan(
        {
            "controller": {"stalled": False},
            "product": {"under_floor": []},
            "chain_heads": [
                {"repo": "omnimarket", "number": 5, "cls": "red", "state": "OPEN"}
            ],
            "reds": [
                {"repo": "omnimarket", "number": 2, "state": "OPEN"},
                {"repo": "omnimarket", "number": 5, "state": "OPEN"},
            ],
        }
    )
    assert [p.pr for p in plan.lanes[0].prs] == ["omnimarket#5", "omnimarket#2"]


def test_a_fix_lane_holds_at_most_five_prs() -> None:
    reds = [{"repo": "omnimarket", "number": n, "state": "OPEN"} for n in range(1, 13)]
    plan = _plan(
        {"controller": {"stalled": False}, "product": {"under_floor": []}, "reds": reds}
    )
    assert [len(lane.prs) for lane in plan.lanes] == [5, 5, 2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 3), (0, 0), (-4, 0), (2, 2), (6, 6), (7, 6)],
)
def test_retries_are_clamped(raw: int | None, expected: int) -> None:
    result = HandlerMergeSweepRetry().handle(
        ModelMergeSweepRetryRequest(exit_code=0, retries=raw)
    )
    assert result.retries == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 5), (0, 5), (-2, 1), (7, 7), (30, 30), (31, 30)],
)
def test_the_wait_is_clamped(raw: int | None, expected: int) -> None:
    result = HandlerMergeSweepRetry().handle(
        ModelMergeSweepRetryRequest(exit_code=75, retry_wait_min=raw)
    )
    assert result.retry_wait_min == expected
    assert result.wait_min == expected


def test_a_spent_retry_budget_accepts_the_no_host_receipt() -> None:
    handler = HandlerMergeSweepRetry()
    assert (
        handler.handle(ModelMergeSweepRetryRequest(exit_code=75, waits_used=3)).action
        == "accept"
    )
    assert (
        handler.handle(ModelMergeSweepRetryRequest(exit_code=75, waits_used=2)).action
        == "wait_and_retry"
    )


def test_the_other_host_result_is_final() -> None:
    handler = HandlerMergeSweepRetry()
    for code in (75, 77, 79):
        decision = handler.handle(
            ModelMergeSweepRetryRequest(exit_code=code, host_retry_used=True)
        )
        assert decision.action == "accept"
