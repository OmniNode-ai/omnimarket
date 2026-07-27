# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression test for OMN-15002 silence closure.

``HandlerBuildLoopOrchestrator.handle()`` previously had no top-level
exception handling. Any exception raised outside the per-phase try/except in
``_execute_phase`` (e.g. in ``HandlerBuildLoop.start``/``.advance``, or the
cycle-loop control flow) propagated straight out of ``handle()``. In
production that lands on the omnibase_infra auto-wired consume boundary,
which -- with ``ONEX_BOUNDARY_DLQ_ENABLED`` off (the live dev/prod/judge
default) -- logs and swallows it: the Kafka offset still commits and
NOTHING is emitted on any of this contract's 11 publish topics. That is a
committed-but-silent loss indistinguishable from the OMN-15002 routing bug
this same ticket fixes.

This test drives exactly that failure path (a real exception escaping the
per-phase try/except, not a happy-path phase failure that the FSM already
handles) through the real ``handle()`` implementation and a real
``EventBusInmemory``, and asserts:

1. The original exception still propagates (callers/the runtime boundary
   still see it -- this is NOT converted into a swallowed success).
2. A ``build-loop-failed`` terminal event is published on the real event bus
   BEFORE the exception propagates, carrying the correlation_id and error
   detail -- so a queryable terminal signal exists even if the platform
   boundary above this handler still swallows the re-raised exception.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    TOPIC_BUILD_LOOP_FAILED,
    HandlerBuildLoopOrchestrator,
)


class _BrokenFsm:
    """A fake FSM whose .start() raises -- escapes _execute_phase's
    try/except entirely, reproducing the "exception outside phase
    execution" failure class that was previously silent end-to-end.
    """

    def start(self, command: object) -> Any:
        msg = "synthetic FSM failure (OMN-15002 regression harness)"
        raise RuntimeError(msg)

    def advance(self, *args: object, **kwargs: object) -> Any:  # pragma: no cover
        msg = "advance() must not be called when start() raises"
        raise AssertionError(msg)


class _NoOpSubHandler:
    """Minimal stand-in; never invoked because start() raises first."""

    async def handle(self, *args: object, **kwargs: object) -> Any:  # pragma: no cover
        msg = "sub-handler must not be invoked when start() raises"
        raise AssertionError(msg)


def _make_command(correlation_id: UUID) -> ModelLoopStartCommand:
    return ModelLoopStartCommand(
        correlation_id=correlation_id,
        max_cycles=1,
        skip_closeout=False,
        dry_run=False,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestHandleFailurePathPublishesTerminalEvent:
    async def test_unhandled_exception_still_propagates(
        self, event_bus: EventBusInmemory
    ) -> None:
        """The original exception must not be swallowed by the new handler."""
        await event_bus.start()
        orch = HandlerBuildLoopOrchestrator(
            closeout=_NoOpSubHandler(),
            verify=_NoOpSubHandler(),
            rsd_fill=_NoOpSubHandler(),
            classify=_NoOpSubHandler(),
            dispatch=_NoOpSubHandler(),
            event_bus=cast(ProtocolEventBusPublisher, event_bus),
            fsm=cast(HandlerBuildLoop, _BrokenFsm()),
        )
        command = _make_command(uuid4())

        with pytest.raises(RuntimeError, match="synthetic FSM failure"):
            await orch.handle(command)

        await event_bus.close()

    async def test_terminal_failed_event_is_published_before_reraise(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A build-loop-failed terminal event must exist on the bus even
        though handle() re-raises -- this is the silence-closure proof.
        """
        await event_bus.start()
        correlation_id = uuid4()
        orch = HandlerBuildLoopOrchestrator(
            closeout=_NoOpSubHandler(),
            verify=_NoOpSubHandler(),
            rsd_fill=_NoOpSubHandler(),
            classify=_NoOpSubHandler(),
            dispatch=_NoOpSubHandler(),
            event_bus=cast(ProtocolEventBusPublisher, event_bus),
            fsm=cast(HandlerBuildLoop, _BrokenFsm()),
        )
        command = _make_command(correlation_id)

        with pytest.raises(RuntimeError):
            await orch.handle(command)

        failed_events = await event_bus.get_event_history(
            topic=TOPIC_BUILD_LOOP_FAILED,
        )

        assert len(failed_events) == 1, (
            "Expected exactly one build-loop-failed terminal event -- an "
            "unhandled exception in handle() must always leave a terminal "
            "trace (OMN-15002 silence closure), not zero events."
        )
        payload = json.loads(failed_events[0].value)
        assert payload["correlation_id"] == str(correlation_id)
        assert payload["error_type"] == "RuntimeError"
        assert "synthetic FSM failure" in payload["error_message"]

        await event_bus.close()
