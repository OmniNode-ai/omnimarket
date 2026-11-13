# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326: a delegation's execution budget is a bound, give or take a declared grace.

Measured on the deployed lane over three days: of 1440 runs budgeted 50 s, 154
ran longer (to 105.8 s), and every run budgeted 1 s completed after 7.6 to 28.3 s.
The handler waited ``execution_timeout_seconds`` plus the task class's
``terminal_delivery_margin_seconds`` -- 60 s on every class -- and the deployed
downstream does not stop at the budget, so a run could finish a full minute over
it and still report completed.

The margin stays for a run whose timeout nobody named: there it also carries a
second escalation rung inside the ceiling. A caller who names N now gets N plus
``requested_timeout_grace_seconds``, declared per task class in
``task_class_contracts.v1.yaml`` and named on the timeout terminal. These tests
hold the grace to a delivery allowance (``MAX_GRACE_SECONDS``) and drive the
handler under a fake clock: a downstream that answers after N + grace + 1 seconds
ends as a typed timeout no later than N + grace, and one that answers inside N
completes unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable
from types import ModuleType
from typing import Any

import pytest

from omnimarket.inference.task_class_authority import (
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
)

pytestmark = pytest.mark.unit

#: A terminal crosses the bus in seconds; the grace exists for that and nothing more.
MAX_GRACE_SECONDS = 15
TASK_CLASS = "document"
BUDGET_SECONDS = 50
ANSWER = "The configuration is inspected first, then changed, then tested."


class _FakeClock:
    """A clock that starts ahead of the real monotonic clock, on any host.

    OMN-24114: a loop on another thread schedules its timers on the real clock;
    were this clock ever read there, every such timer would be due at once.
    Starting ahead of the real clock makes that true on a host that has been up
    for days, as on one that has only just started.
    """

    def __init__(self) -> None:
        self.now = time.monotonic() + 1000.0

    def monotonic(self) -> float:
        return self.now


class _Downstream:
    """A dispatch port whose answer arrives ``delay`` seconds after dispatch."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def dispatch(self, **_: Any) -> dict[str, Any]:
        return {"status": "completed", "content": ANSWER}


class _ModuleWithOverrides:
    """A module as the handler sees it, a few attributes replaced, every other attribute read from the real module."""

    def __init__(self, real: ModuleType, **overrides: Any) -> None:
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


def _install_clock(
    monkeypatch: pytest.MonkeyPatch, downstream: _Downstream
) -> _FakeClock:
    """``asyncio.wait_for`` and ``time.monotonic`` on one fake clock, scoped to the handler module so no other thread reaches them."""
    clock = _FakeClock()

    async def fake_wait_for(aw: Awaitable[Any], timeout: float | None) -> Any:
        assert timeout is not None
        if downstream.delay > timeout:
            clock.now += timeout
            if asyncio.iscoroutine(aw):
                aw.close()
            raise TimeoutError
        clock.now += downstream.delay
        return await aw

    monkeypatch.setattr(
        handler_mod,
        "time",
        _ModuleWithOverrides(handler_mod.time, monotonic=clock.monotonic),
    )
    monkeypatch.setattr(
        handler_mod,
        "asyncio",
        _ModuleWithOverrides(handler_mod.asyncio, wait_for=fake_wait_for),
    )
    return clock


def _start_retry_loop_thread() -> tuple[threading.Thread, threading.Event]:
    """The shape of the helper thread that earlier projection tests leave alive.

    A daemon thread owns its own event loop and runs a task shaped like
    ``AggregateCoalescer._flush_loop``: it waits on an event with a short
    ``asyncio.wait_for`` timeout, retrying until a stop event is set.
    """
    stop_event = threading.Event()
    started = threading.Event()

    async def _loop() -> None:
        event = asyncio.Event()
        started.set()
        while not stop_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=0.005)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_loop())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    return thread, stop_event


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="Plan a configuration change.",
        task_type=TASK_CLASS,
        source="claude-code",
        requested_timeout_seconds=BUDGET_SECONDS,
    )


def _grace() -> int:
    grace = resolve_task_class_execution_budget(
        TASK_CLASS
    ).requested_timeout_grace_seconds
    assert grace is not None
    return grace


def test_every_task_class_declares_a_grace_no_wider_than_delivery() -> None:
    from omnimarket.inference.task_class_authority import load_task_class_authority

    authority = load_task_class_authority()
    graces = {
        name: authority.execution_budget(name).requested_timeout_grace_seconds
        for name in authority.task_classes
        if _has_budget(authority, name)
    }
    bad = {
        name: grace
        for name, grace in graces.items()
        if grace is None or grace > MAX_GRACE_SECONDS
    }
    assert graces
    assert not bad, f"undeclared or wider than {MAX_GRACE_SECONDS}s: {bad}"


def _has_budget(authority: Any, name: str) -> bool:
    try:
        authority.execution_budget(name)
    except ValueError:
        return False
    return True


async def test_a_run_past_its_budget_and_grace_ends_as_a_typed_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: the downstream answers at N + grace + 1; the run ends a timeout by N + grace."""
    downstream = _Downstream(delay=BUDGET_SECONDS + MAX_GRACE_SECONDS + 1)
    _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    response = await handler.handle(_request())

    assert response.status != "completed"
    cause = getattr(response, "terminal_failure_cause", None)
    assert cause is not None
    assert str(getattr(cause, "value", cause)) == "timeout"
    assert response.execution_duration_ms is not None
    assert response.execution_duration_ms <= (BUDGET_SECONDS + _grace()) * 1000
    assert response.budget_evidence is not None
    assert response.budget_evidence.requested_timeout_seconds == BUDGET_SECONDS
    assert f"{_grace()}s allowance" in (response.error_message or "")


async def test_a_run_inside_its_budget_completes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: the downstream answers at N - 1; the run completes with its answer."""
    downstream = _Downstream(delay=BUDGET_SECONDS - 1)
    _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    response = await handler.handle(_request())

    assert response.status == "completed"
    assert ANSWER in (getattr(response, "content", None) or str(response))
    assert response.execution_duration_ms == (BUDGET_SECONDS - 1) * 1000


async def test_a_retry_loop_alive_on_another_thread_cannot_move_the_fake_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-24114: a helper thread retrying asyncio.wait_for must not change the duration the handler reports."""
    thread, stop = _start_retry_loop_thread()
    downstream = _Downstream(delay=BUDGET_SECONDS - 1)
    _install_clock(monkeypatch, downstream)
    handler = HandlerDelegateSkill(object(), dispatch_port=downstream)

    try:
        response = await handler.handle(_request())
    finally:
        stop.set()
        thread.join(timeout=5)

    assert response.status == "completed"
    assert response.execution_duration_ms == (BUDGET_SECONDS - 1) * 1000
