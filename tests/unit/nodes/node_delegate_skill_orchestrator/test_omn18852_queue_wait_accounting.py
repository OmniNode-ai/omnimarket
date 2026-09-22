# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18852 AC3: queue wait and execution are separate facts on the terminal.

Measured on the ``.201`` dev lane, 2026-09-19: nine delegations in 26 minutes
against a service rate of one inference at a time. Queue wait -- the interval
between the command record being PUBLISHED and this handler PICKING IT UP --
grew monotonically from 3 s to 445 s. A control run took 181 s wall clock of
which the inference was 1.559 s: 99 % of the caller's wait was queue, and
nothing on the terminal said so.

Two distinct facts were collapsed into one number a caller could only read as
"slow":

* ``queue_wait_ms``  -- record publish -> handler pickup. Time the work had
  not started.
* ``execution_duration_ms`` -- pickup -> terminal. Time the work was running.

Both are OPTIONAL. Absent means NOT MEASURED, never zero: a producer that does
not stamp ``published_at`` cannot have its queue wait derived, and reporting
``0`` there would assert an empty queue that was never observed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.inference.task_class_authority import ModelTaskClassExecutionBudget
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as handler_delegate_skill_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)


class _FastDispatchPort:
    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


class _NeverReturningDispatchPort:
    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _request(
    published_at: datetime | None = None,
    *,
    requested_timeout_seconds: int | None = None,
) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a test",
        task_type="code_generation",
        source="claude-code",
        correlation_id=uuid4(),
        published_at=published_at,
        requested_timeout_seconds=requested_timeout_seconds,
    )


@pytest.fixture
def _one_second_execution_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a short explicit execution and delivery budget for timeout cases."""
    monkeypatch.setattr(
        handler_delegate_skill_module,
        "resolve_task_class_execution_budget",
        lambda _task_type: ModelTaskClassExecutionBudget(
            task_class_timeout_ceiling_seconds=1,
            terminal_delivery_margin_seconds=1,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queue_wait_and_execution_are_separately_readable() -> None:
    """RED before the fix: neither field exists on the terminal at all."""
    published_at = datetime.now(UTC) - timedelta(seconds=12)
    handler = HandlerDelegateSkill(dispatch_port=_FastDispatchPort())  # type: ignore[arg-type]

    terminal = await handler.handle(_request(published_at))

    assert terminal.status == "completed"
    assert terminal.queue_wait_ms is not None
    assert terminal.execution_duration_ms is not None
    # 12 s of queue, well under a second of work. The two must not be one
    # number: a caller reading only wall clock cannot tell them apart.
    assert terminal.queue_wait_ms >= 12_000
    assert terminal.execution_duration_ms < 5_000
    assert terminal.queue_wait_ms > terminal.execution_duration_ms


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_fast_job_behind_a_long_queue_is_not_failed_for_the_queue() -> None:
    """The measured failure, as a test.

    Correlation ``b16b1c53`` waited 106 s in queue before pickup. The handler
    budget must be measured from PICKUP, so a ten-minute queue in front of
    50 ms of work still completes.
    """
    published_at = datetime.now(UTC) - timedelta(seconds=600)
    handler = HandlerDelegateSkill(dispatch_port=_FastDispatchPort())  # type: ignore[arg-type]

    terminal = await handler.handle(_request(published_at, requested_timeout_seconds=2))

    assert terminal.status == "completed"
    assert terminal.queue_wait_ms is not None
    assert terminal.queue_wait_ms >= 600_000
    assert terminal.execution_duration_ms is not None
    assert terminal.execution_duration_ms < 2_000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unstamped_request_reports_queue_wait_as_not_measured() -> None:
    """Absent is not zero. A producer that stamped nothing measured nothing."""
    handler = HandlerDelegateSkill(dispatch_port=_FastDispatchPort())  # type: ignore[arg-type]

    terminal = await handler.handle(_request())

    assert terminal.queue_wait_ms is None
    assert terminal.execution_duration_ms is not None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_one_second_execution_budget")
async def test_the_budget_refusal_reports_the_measured_queue_wait() -> None:
    """A reader must be able to tell a slow job from a queued one.

    The pre-fix refusal said only that the 240 s budget was exceeded, which is
    the same sentence for a job that ran 240 s and for one that sat 445 s in a
    queue and then ran 3 s.
    """
    published_at = datetime.now(UTC) - timedelta(seconds=30)
    handler = HandlerDelegateSkill(dispatch_port=_NeverReturningDispatchPort())  # type: ignore[arg-type]

    terminal = await asyncio.wait_for(
        handler.handle(_request(published_at, requested_timeout_seconds=1)), timeout=15
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "timeout"
    assert terminal.queue_wait_ms is not None
    assert terminal.queue_wait_ms >= 30_000
    assert "queue wait" in terminal.error_message
    assert str(terminal.queue_wait_ms) in terminal.error_message
    # The budget itself is still named -- this adds a fact, it removes none.
    assert "budget" in terminal.error_message
    assert "1s" in terminal.error_message


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_one_second_execution_budget")
async def test_the_budget_refusal_claims_no_queue_wait_when_none_was_measured() -> None:
    """Positive control: the refusal must not invent a queue it never saw."""
    handler = HandlerDelegateSkill(dispatch_port=_NeverReturningDispatchPort())  # type: ignore[arg-type]

    terminal = await asyncio.wait_for(
        handler.handle(_request(requested_timeout_seconds=1)), timeout=15
    )

    assert terminal.status == "timeout"
    assert terminal.queue_wait_ms is None
    assert "queue wait" not in terminal.error_message
    assert terminal.execution_duration_ms is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_dispatch_exception_still_carries_the_accounting() -> None:
    """The error chain reports the same two facts as the success chain."""

    class _RaisingPort:
        async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
            raise RuntimeError("backend unavailable")

    published_at = datetime.now(UTC) - timedelta(seconds=7)
    handler = HandlerDelegateSkill(dispatch_port=_RaisingPort())  # type: ignore[arg-type]

    terminal = await handler.handle(_request(published_at))

    assert terminal.status == "failed"
    assert terminal.queue_wait_ms is not None
    assert terminal.queue_wait_ms >= 7_000
    assert terminal.execution_duration_ms is not None


@pytest.mark.unit
def test_a_naive_published_at_is_refused_rather_than_guessed() -> None:
    """A timestamp with no zone is a measurement with no meaning."""
    with pytest.raises(ValueError, match="timezone-aware"):
        ModelDelegateSkillRequest(
            prompt="write a test",
            task_type="code_generation",
            source="claude-code",
            published_at=datetime(2026, 9, 19, 19, 49, 43),
        )
