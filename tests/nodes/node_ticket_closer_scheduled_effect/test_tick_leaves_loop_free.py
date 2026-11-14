# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loop-freedom of the scheduled ticket closer's runtime tick (OMN-19998).

The runtime calls a typed handler's ``handle`` on its event loop, and the closer's run waits on the ticket node
(up to the overlay's timeout per call), so the tick must run it off the loop; the ticket node answers inside the
same runtime, so a run that holds the loop can never be answered. A verifier that blocks its thread on a release
event stands in for a waiting ticket call. The controls show the checks can fail: the same run on the loop
thread reads the loop held, the in-flight guard cleared reads a second run, and a plain function is not a coroutine.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import timedelta

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.ticket_closer import (
    ModelCloserCandidate,
    ModelCloserVerifyReading,
    ModelTicketCloserResult,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled import (
    HandlerTicketCloserScheduled,
)
from tests.nodes.node_ticket_closer_scheduled_effect.test_golden_chain_ticket_closer import (
    T0,
    Source,
    Writer,
    handler,
    schedule,
    tick,
)

pytestmark = pytest.mark.unit


class _BlockingVerifier:
    """A verifier whose verify blocks its thread on a release event, as a ticket call waiting for the ticket node does."""

    def __init__(self, wait_seconds: float = 2.0) -> None:
        self.released = threading.Event()
        self.wait_seconds = wait_seconds
        self.calls = 0
        self.waited_ok = False

    def verify(
        self, ticket_id: str, execution_audience: str
    ) -> ModelCloserVerifyReading:
        self.calls += 1
        self.waited_ok = self.released.wait(timeout=self.wait_seconds)
        return ModelCloserVerifyReading(
            ticket_id=ticket_id, ran=False, error_code="VERIFIER_PARKED"
        )


class _RaisingSource:
    def candidates(self, now: object, limit: int) -> list[ModelCloserCandidate] | None:
        raise RuntimeError("source failed")


class _OnLoopCloser(HandlerTicketCloserScheduled):
    """The shape before the change: the whole run on the loop thread."""

    async def handle(self, request: ModelRuntimeTick) -> ModelTicketCloserResult | None:
        return self._run(request, self._schedule_overlay(), request.now)


def _candidate() -> ModelCloserCandidate:
    return ModelCloserCandidate(
        ticket_id="OMN-1", criteria=("AC1",), all_prs_merged=True, runtime_proof=None
    )


def _after_the_interval() -> timedelta:
    return timedelta(seconds=schedule().run_interval_seconds + 1)


async def test_the_tick_leaves_the_event_loop_free_while_the_verifier_waits() -> None:
    verifier = _BlockingVerifier()
    closer = handler(Source([_candidate()]), verifier, Writer())

    task = asyncio.create_task(closer.handle(tick(T0)))
    await asyncio.sleep(0.05)
    verifier.released.set()
    result = await task

    assert verifier.waited_ok is True
    assert result is not None
    assert result.examined == 1


async def test_control_a_run_on_the_loop_thread_holds_the_loop() -> None:
    verifier = _BlockingVerifier(wait_seconds=0.3)
    closer = _OnLoopCloser(
        schedule=schedule(), source=Source([_candidate()]), verifier=verifier
    )

    task = asyncio.create_task(closer.handle(tick(T0)))
    await asyncio.sleep(0.05)
    verifier.released.set()
    await task

    assert verifier.waited_ok is False


async def test_a_tick_after_the_interval_during_a_run_starts_no_second_run() -> None:
    verifier = _BlockingVerifier()
    closer = handler(Source([_candidate()]), verifier, Writer())

    first = asyncio.create_task(closer.handle(tick(T0)))
    await asyncio.sleep(0.05)
    second = await closer.handle(tick(T0 + _after_the_interval()))
    verifier.released.set()
    first_result = await first

    assert second is None
    assert first_result is not None
    assert verifier.calls == 1


async def test_control_with_the_in_flight_flag_cleared_a_second_run_starts() -> None:
    verifier = _BlockingVerifier()
    closer = handler(Source([_candidate()]), verifier, Writer())

    first = asyncio.create_task(closer.handle(tick(T0)))
    await asyncio.sleep(0.05)
    closer._running = False
    second = asyncio.create_task(closer.handle(tick(T0 + _after_the_interval())))
    await asyncio.sleep(0.05)
    verifier.released.set()
    await asyncio.gather(first, second)

    assert verifier.calls == 2


async def test_error_a_run_that_raises_releases_the_in_flight_flag() -> None:
    closer = HandlerTicketCloserScheduled(
        schedule=schedule(), source=_RaisingSource(), verifier=_BlockingVerifier()
    )

    with pytest.raises(RuntimeError, match="source failed"):
        await closer.handle(tick(T0))
    assert closer._running is False

    closer = handler(Source([_candidate()]), _BlockingVerifier(0.1), Writer())
    assert await closer.handle(tick(T0)) is not None


def test_handle_is_a_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(HandlerTicketCloserScheduled.handle) is True


def test_control_a_plain_function_is_not_a_coroutine_function() -> None:
    def plain(request: ModelRuntimeTick) -> None:
        return None

    assert inspect.iscoroutinefunction(plain) is False
