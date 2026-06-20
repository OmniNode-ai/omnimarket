# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13361: deterministic proof that `_wait_for_terminal_event` bounds on timeout.

The e2e probe's `_wait_for_terminal_event` previously relied on aiokafka's
`consumer_timeout_ms` to break `async for msg in consumer`. That parameter does
NOT stop `__anext__` on an idle topic, so the wait blocked forever and the
`TimeoutError` was unreachable — the test hung, and `TestE2E4BTerminalEvent` was
skipped to avoid it.

These tests patch `AIOKafkaConsumer` with a fake consumer (no live bus needed) so
they run in CI, and prove:

1. When no terminal event arrives, the wait raises `TimeoutError` within the
   configured bound instead of hanging.
2. When a matching terminal event arrives, the wait returns it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from tests.integration.e2e_probe.test_delegation_e2e_probe import (
    _wait_for_terminal_event,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_CONSUMER_PATH = "aiokafka.AIOKafkaConsumer"


class _FakeMessage:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


class _NeverYieldingConsumer:
    """Async-iterates forever without yielding — the idle-topic failure mode."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def __aiter__(self) -> _NeverYieldingConsumer:
        return self

    async def __anext__(self) -> _FakeMessage:
        # Mirror aiokafka's idle behaviour: block indefinitely, never raising
        # StopAsyncIteration until the consumer is explicitly stopped.
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _OneShotConsumer:
    """Yields exactly one matching terminal event, then would block forever."""

    _pending: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._value: dict[str, Any] | None = dict(_OneShotConsumer._pending)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def __aiter__(self) -> _OneShotConsumer:
        return self

    async def __anext__(self) -> _FakeMessage:
        if self._value is not None:
            value = self._value
            self._value = None
            return _FakeMessage(value)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_wait_for_terminal_event_times_out_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No terminal event -> TimeoutError raised within the bound, not a hang."""
    monkeypatch.setattr(_CONSUMER_PATH, _NeverYieldingConsumer)

    correlation_id = str(uuid.uuid4())
    timeout = 0.5
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        # Wrap in an outer guard far larger than the bound; if the inner timeout
        # were ineffective (the original bug), this outer guard would trip and the
        # test would fail loudly rather than hang the suite.
        async with asyncio.timeout(timeout + 5.0):
            await _wait_for_terminal_event(correlation_id, timeout=timeout)
    elapsed = time.monotonic() - start
    assert elapsed < timeout + 2.0, (
        f"wait did not bound on timeout: elapsed={elapsed:.2f}s bound={timeout}s"
    )


@pytest.mark.asyncio
async def test_wait_for_terminal_event_returns_matching_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching terminal event is returned before the timeout fires."""
    correlation_id = str(uuid.uuid4())
    _OneShotConsumer._pending = {
        "correlation_id": correlation_id,
        "event_type": "onex.evt.omnimarket.projection-delegation-applied.v1",
        "payload": {"projected": True, "correlation_id": correlation_id},
    }
    monkeypatch.setattr(_CONSUMER_PATH, _OneShotConsumer)

    result = await _wait_for_terminal_event(correlation_id, timeout=5.0)
    assert result["correlation_id"] == correlation_id
