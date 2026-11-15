# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loop-freedom of the convergence orchestrator's runtime tick (OMN-24421).

The handler's dispatch blocks until each lab host's receipt, so the tick must run it off the
runtime's event loop; a blocking send on the loop thread is the failing control that shows
the detector catches the regression.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from omnimarket.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.dispatch_convergence import (
    DispatchConvergenceUnits,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.sender_in_memory import (
    InMemoryConvergenceSender,
)
from tests.unit.nodes.node_convergence_orchestrator.test_scheduled_run import (
    T0,
    handler,
    overlay,
    tick,
)

pytestmark = pytest.mark.unit


class _BlockingSender(InMemoryConvergenceSender):
    """A sender whose send blocks on a release event, recording whether it was released in time."""

    def __init__(self) -> None:
        super().__init__()
        self.released = threading.Event()
        self.waited_ok = False

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.waited_ok = self.released.wait(timeout=2.0)
        return super().send(request)


async def _release_after_a_turn(sender: _BlockingSender) -> None:
    await asyncio.sleep(0)
    sender.released.set()


async def test_the_tick_leaves_the_event_loop_free_while_the_send_blocks() -> None:
    sender = _BlockingSender()
    h = handler(sender)

    task = asyncio.create_task(h.handle(tick(T0)))
    await asyncio.sleep(0.05)
    sender.released.set()
    output = await task

    assert sender.waited_ok is True
    assert len(output.events) == 2


async def test_control_a_send_run_on_the_loop_thread_starves_the_loop() -> None:
    sender = _BlockingSender()
    o = overlay()
    request = DispatchConvergenceUnits(o, sender).request_for(o.hosts[0])

    heartbeat = asyncio.create_task(_release_after_a_turn(sender))
    sender.send(request)
    await heartbeat

    assert sender.waited_ok is False
