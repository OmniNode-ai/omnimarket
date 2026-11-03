# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22223: the runtime dispatch timeout and the caller's wait are one budget on one clock.

The caller waits ``execution + terminal_delivery_margin`` from PUBLISH. The runtime
used to time the same sum from PICKUP, so a record that queued for 120 s ran its
full 300 s and its terminal landed 120 s after the caller had stopped waiting
(158 of 6916 deployed-lane runs ended at the caller's 300 s wait with no terminal).
The runtime deadline now deducts the measured queue wait, and a record that spent
the whole budget in the queue ends with a typed ``queue_budget_exhausted`` terminal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.inference.task_class_authority import (
    load_task_class_authority,
    resolve_task_class_execution_budget,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as handler_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    remaining_budget_seconds,
    resolve_dispatch_window_seconds,
)

pytestmark = pytest.mark.unit

TASK_CLASS = "document"
TOTAL_BUDGET_SECONDS = 300
ANSWER = "The configuration is inspected first, then changed, then tested."


class _Downstream:
    """A dispatch port that records the call and answers ``delay`` seconds in."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.called = False

    async def dispatch(self, **_: Any) -> dict[str, Any]:
        self.called = True
        return {"status": "completed", "content": ANSWER}


def _install_clock(
    monkeypatch: pytest.MonkeyPatch, downstream: _Downstream
) -> list[float]:
    """A fake clock under ``wait_for``; returns the timeouts the handler asked for."""
    now = [1000.0]
    asked: list[float] = []
    monkeypatch.setattr(handler_mod.time, "monotonic", lambda: now[0])

    async def fake_wait_for(aw: Awaitable[Any], timeout: float | None) -> Any:
        assert timeout is not None
        asked.append(timeout)
        if downstream.delay > timeout:
            now[0] += timeout
            if asyncio.iscoroutine(aw):
                aw.close()
            raise TimeoutError
        now[0] += downstream.delay
        return await aw

    monkeypatch.setattr(handler_mod.asyncio, "wait_for", fake_wait_for)
    return asked


def _request(queue_wait_seconds: float | None) -> ModelDelegateSkillRequest:
    published_at = (
        None
        if queue_wait_seconds is None
        else datetime.now(UTC) - timedelta(seconds=queue_wait_seconds)
    )
    return ModelDelegateSkillRequest(
        prompt="Plan a configuration change.",
        task_type=TASK_CLASS,
        source="claude-code",
        published_at=published_at,
    )


def test_the_declared_budget_for_the_class_is_three_hundred_seconds() -> None:
    budget = resolve_task_class_execution_budget(TASK_CLASS)
    assert (
        budget.task_class_timeout_ceiling_seconds
        + budget.terminal_delivery_margin_seconds
        == TOTAL_BUDGET_SECONDS
    )


async def test_queue_wait_deducted_from_the_dispatch_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: 120 s in the queue of a 300 s budget leaves 180 s, not 300 s, to run."""
    downstream = _Downstream(delay=TOTAL_BUDGET_SECONDS - 1)
    asked = _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    response = await handler.handle(_request(queue_wait_seconds=120))

    assert len(asked) == 1
    assert 179.0 <= asked[0] <= 180.0, asked
    assert response.status != "completed"
    cause = getattr(response, "terminal_failure_cause", None)
    assert str(getattr(cause, "value", cause)) == "timeout"
    assert response.execution_duration_ms is not None
    assert response.execution_duration_ms <= 180_000
    assert response.queue_wait_ms is not None
    assert response.queue_wait_ms >= 120_000
    assert "measured from publish" in (response.error_message or "")


async def test_queue_wait_deducted_leaves_a_fast_run_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that answers inside what is left of the budget completes."""
    downstream = _Downstream(delay=100)
    _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    response = await handler.handle(_request(queue_wait_seconds=120))

    assert response.status == "completed"


async def test_queue_wait_deducted_unstamped_request_keeps_the_full_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stamp, no measurement: nothing is deducted (not booked as zero or spent)."""
    downstream = _Downstream(delay=10)
    asked = _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    response = await handler.handle(_request(queue_wait_seconds=None))

    assert asked == [float(TOTAL_BUDGET_SECONDS)]
    assert response.status == "completed"


async def test_queue_wait_deducted_a_spent_budget_ends_typed_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a record that queued the whole budget ends typed, with nothing dispatched."""
    downstream = _Downstream(delay=1)
    asked = _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    response = await handler.handle(
        _request(queue_wait_seconds=TOTAL_BUDGET_SECONDS + 5)
    )

    assert response.status == "failed"
    assert not downstream.called
    assert asked == []
    assert (response.error_message or "").startswith("queue_budget_exhausted:")
    cause = getattr(response, "terminal_failure_cause", None)
    assert str(getattr(cause, "value", cause)) == "refused_before_dispatch"
    assert response.queue_wait_ms is not None
    assert response.queue_wait_ms >= TOTAL_BUDGET_SECONDS * 1000
    assert response.execution_duration_ms == 0
    assert response.budget_evidence is not None


def test_remaining_budget_arithmetic() -> None:
    assert remaining_budget_seconds(300, None) == 300.0
    assert remaining_budget_seconds(300, 0) == 300.0
    assert remaining_budget_seconds(300, 120_000) == 180.0
    assert remaining_budget_seconds(300, 300_000) == 0.0
    assert remaining_budget_seconds(300, 445_000) < 0


def _task_classes_with_budgets() -> list[str]:
    authority = load_task_class_authority()
    names: list[str] = []
    for name in authority.task_classes:
        try:
            authority.execution_budget(name)
        except ValueError:
            continue
        names.append(name)
    return names


def test_caller_wait_covers_runtime_for_every_task_class() -> None:
    """AC2: the caller's wait is never shorter than the runtime's dispatch deadline."""
    from omnibase_infra.cli.cli_delegate import _terminal_wait_seconds

    names = _task_classes_with_budgets()
    assert names
    checked = 0
    for name in names:
        budget = resolve_task_class_execution_budget(name)
        ceiling = budget.task_class_timeout_ceiling_seconds
        for requested in (None, 1, 30, 50, ceiling // 2, ceiling):
            execution, allowance = resolve_dispatch_window_seconds(
                requested_timeout_seconds=requested,
                ceiling_seconds=ceiling,
                margin_seconds=budget.terminal_delivery_margin_seconds,
                grace_seconds=getattr(budget, "requested_timeout_grace_seconds", None),
            )
            caller = _terminal_wait_seconds(
                requested_timeout_seconds=requested, execution_budget=budget
            )
            assert caller >= execution + allowance, (name, requested, caller)
            checked += 1
    assert checked >= len(names) * 6


def test_caller_wait_covers_runtime_even_when_a_grace_is_declared_wider_than_the_margin() -> (
    None
):
    """AC2: a misdeclared grace cannot push the runtime deadline past the caller's wait."""
    execution, allowance = resolve_dispatch_window_seconds(
        requested_timeout_seconds=50,
        ceiling_seconds=240,
        margin_seconds=60,
        grace_seconds=200,
    )
    assert execution + allowance <= 50 + 60
