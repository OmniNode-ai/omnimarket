# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22223: the caller wait and the runtime dispatch timeout are one budget.

Measured over 6916 runs: 158 deployed-lane runs ended at the caller's 300 s
wait with no terminal and no rung. The runtime timed its execution budget from
PICKUP while the caller timed the same sum from PUBLISH, so a run that sat in
the queue still got its full budget after pickup and outlived the caller.

The runtime budget is now measured from publish: queue wait is deducted, a run
that spent its whole execution window in the queue ends with a typed
``queue_budget_exhausted`` terminal, and the runtime's wait never exceeds the
caller's wait for the same request.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.cli.cli_delegate import _terminal_wait_seconds

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

pytestmark = pytest.mark.unit

CEILING = 240
MARGIN = 60


class _RecordingPort:
    """Records what the handler hands the port, then fails the run fast."""

    def __init__(self) -> None:
        self.calls = 0
        self.execution_timeout_seconds: int | None = None

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.calls += 1
        self.execution_timeout_seconds = kwargs["execution_timeout_seconds"]
        raise RuntimeError("stub run: nothing to dispatch")


def _request(
    *, queue_wait_seconds: float | None, requested_timeout_seconds: int | None = None
) -> ModelDelegateSkillRequest:
    published_at = (
        None
        if queue_wait_seconds is None
        else datetime.now(UTC) - timedelta(seconds=queue_wait_seconds)
    )
    return ModelDelegateSkillRequest(
        prompt="summarise the log",
        task_type="document",
        source="claude-code",
        correlation_id=uuid4(),
        published_at=published_at,
        requested_timeout_seconds=requested_timeout_seconds,
    )


def _handler(
    monkeypatch: pytest.MonkeyPatch, port: Any, *, grace: int | None = None
) -> tuple[HandlerDelegateSkill, Any]:
    budget = SimpleNamespace(
        task_class_timeout_ceiling_seconds=CEILING,
        terminal_delivery_margin_seconds=MARGIN,
        requested_timeout_grace_seconds=grace,
    )
    monkeypatch.setattr(
        handler_delegate_skill, "resolve_task_class_execution_budget", lambda _t: budget
    )
    return HandlerDelegateSkill(dispatch_port=port), budget


def _spy_wait_for(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the timeout of every ``asyncio.wait_for`` the handler opens."""
    timeouts: list[float] = []
    real = asyncio.wait_for

    async def spy(awaitable: Any, timeout: float | None) -> Any:
        assert timeout is not None
        timeouts.append(timeout)
        return await real(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    return timeouts


@pytest.mark.asyncio
async def test_queue_wait_deducted_from_the_runtime_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """120 s in the queue of a 300 s budget leaves 180 s, not 300 s."""
    port = _RecordingPort()
    handler, _ = _handler(monkeypatch, port)
    timeouts = _spy_wait_for(monkeypatch)

    await handler.handle(_request(queue_wait_seconds=120))

    assert len(timeouts) == 1
    assert timeouts[0] == pytest.approx(CEILING + MARGIN - 120, abs=2), (
        "OMN-22223: the runtime timed its budget from pickup and ran the full "
        f"{CEILING + MARGIN} s after 120 s of queue wait"
    )
    assert port.execution_timeout_seconds is not None
    assert abs(port.execution_timeout_seconds - (CEILING - 120)) <= 2


@pytest.mark.asyncio
async def test_unstamped_request_keeps_the_full_budget_queue_wait_deducted_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No publish stamp means no measurement, so nothing is deducted."""
    port = _RecordingPort()
    handler, _ = _handler(monkeypatch, port)
    timeouts = _spy_wait_for(monkeypatch)

    await handler.handle(_request(queue_wait_seconds=None))

    assert timeouts == [float(CEILING + MARGIN)]
    assert port.execution_timeout_seconds == CEILING


@pytest.mark.asyncio
async def test_queue_wait_deducted_exhausted_budget_ends_with_a_typed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that spent its execution window in the queue never dispatches."""
    port = _RecordingPort()
    handler, _ = _handler(monkeypatch, port)
    request = _request(queue_wait_seconds=CEILING + 10)

    terminal = await handler.handle(request)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.correlation_id == request.correlation_id
    assert (terminal.error_message or "").startswith("queue_budget_exhausted:")
    assert terminal.terminal_failure_cause is not None
    assert terminal.terminal_failure_cause.value == "refused_before_dispatch"
    assert terminal.queue_wait_ms is not None
    assert terminal.queue_wait_ms >= (CEILING + 10) * 1000
    assert terminal.attempts_count == 0
    assert port.calls == 0, "no rung may run on a budget the queue already spent"


@pytest.mark.parametrize("queue_wait_seconds", [None, 0, 90, 200])
@pytest.mark.parametrize("requested_timeout_seconds", [None, 100, CEILING])
@pytest.mark.parametrize("grace", [None, 15, 90])
@pytest.mark.asyncio
async def test_caller_wait_covers_runtime_timeout(
    monkeypatch: pytest.MonkeyPatch,
    queue_wait_seconds: float | None,
    requested_timeout_seconds: int | None,
    grace: int | None,
) -> None:
    """The runtime never waits longer than the caller waits for the same request."""
    port = _RecordingPort()
    handler, budget = _handler(monkeypatch, port, grace=grace)
    timeouts = _spy_wait_for(monkeypatch)

    await handler.handle(
        _request(
            queue_wait_seconds=queue_wait_seconds,
            requested_timeout_seconds=requested_timeout_seconds,
        )
    )

    caller_wait = _terminal_wait_seconds(
        requested_timeout_seconds=requested_timeout_seconds,
        execution_budget=budget,
    )
    # A request whose queue wait spent the window opens no wait at all: it
    # ends with the queue-budget terminal before the caller's wait ends.
    assert len(timeouts) <= 1
    assert all(t <= caller_wait for t in timeouts), (
        f"OMN-22223: the runtime waits {timeouts} s but the caller stops at "
        f"{caller_wait} s, so a terminal the runtime records is never seen"
    )
