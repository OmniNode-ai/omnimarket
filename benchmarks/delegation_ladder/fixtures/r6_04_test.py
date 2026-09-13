"""Synchronous by construction: each test drives its own event loop, so the
focused run needs no asyncio plugin and no configuration file."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from subject import LongLivedTerminalCorrelator


def _drive(body: Callable[[Any, Any], Coroutine[Any, Any, Any]]) -> Any:
    loop = asyncio.new_event_loop()
    try:
        correlator = LongLivedTerminalCorrelator(loop)
        return loop.run_until_complete(body(loop, correlator))
    finally:
        loop.close()


def test_a_terminal_arriving_after_the_caller_waits_is_delivered() -> None:
    async def body(loop: Any, correlator: Any) -> Any:
        waiter = asyncio.ensure_future(correlator._wait("c-1", 2.0))
        await asyncio.sleep(0)
        correlator._deliver("c-1", {"status": "completed"})
        return await waiter

    assert _drive(body) == {"status": "completed"}


def test_a_terminal_arriving_before_the_caller_waits_is_still_delivered() -> None:
    async def body(loop: Any, correlator: Any) -> Any:
        correlator._pending["c-2"] = loop.create_future()
        correlator._deliver("c-2", {"status": "completed"})
        return await correlator._wait("c-2", 2.0)

    assert _drive(body) == {"status": "completed"}


def test_a_terminal_nobody_registered_is_dropped_without_raising() -> None:
    async def body(loop: Any, correlator: Any) -> Any:
        correlator._deliver("never-registered", {"status": "completed"})
        return "never-registered" in correlator._pending

    assert _drive(body) is False


def test_a_timeout_returns_none_and_leaves_nothing_behind() -> None:
    async def body(loop: Any, correlator: Any) -> Any:
        result = await correlator._wait("c-3", 0.05)
        return result, "c-3" in correlator._pending

    assert _drive(body) == (None, False)


def test_a_delivered_correlation_is_not_left_pending_afterwards() -> None:
    async def body(loop: Any, correlator: Any) -> Any:
        waiter = asyncio.ensure_future(correlator._wait("c-4", 2.0))
        await asyncio.sleep(0)
        correlator._deliver("c-4", {"status": "completed"})
        await waiter
        return "c-4" in correlator._pending

    assert _drive(body) is False
