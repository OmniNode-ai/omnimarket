# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loop-freedom of the host hygiene orchestrator's runtime tick (OMN-23707).

The handler's dispatch blocks until each lab host's receipt, so the tick must run it off the
runtime's event loop; a blocking send on the loop thread is the failing control that shows
the detector catches the regression.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading

import pytest

from omnimarket.models.model_host_hygiene_orchestrator import (
    ModelHostHygieneOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)
from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
    T0,
    _handler,
    _overlay,
    _store_with_reading,
    _tick,
)

pytestmark = pytest.mark.unit


class _BlockingSender(InMemoryHostHygieneSender):
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


def _request_for(
    overlay: ModelHostHygieneOrchestratorOverlay,
) -> ModelLabWorkUnitRequest:
    host = overlay.hosts[0]
    cleanup = overlay.cleanups[0]
    lab_work = overlay.lab_work
    argv = tuple(
        word.replace("{host}", host.name).replace("{registry_root}", host.registry_root)
        for word in cleanup.argv
    )
    return ModelLabWorkUnitRequest(
        work_unit_id=f"{lab_work.unit_id_prefix}-loop-free-test",
        target_host=host.name,
        repo=lab_work.repo,
        commit_sha=lab_work.commit_sha,
        argv=list(argv),
        kind=cleanup.kind,
        timeout_seconds=cleanup.timeout_seconds,
        lane=lab_work.lane,
    )


async def test_the_tick_leaves_the_event_loop_free_while_the_send_blocks() -> None:
    sender = _BlockingSender()
    handler = _handler(_store_with_reading(90, T0 - dt.timedelta(seconds=30)), sender)

    task = asyncio.create_task(handler.handle(_tick(T0)))
    await asyncio.sleep(0.05)
    sender.released.set()
    output = await task

    assert sender.waited_ok is True
    assert len(output.events) == 2


async def test_control_a_send_run_on_the_loop_thread_starves_the_loop() -> None:
    sender = _BlockingSender()
    request = _request_for(_overlay())

    heartbeat = asyncio.create_task(_release_after_a_turn(sender))
    sender.send(request)
    await heartbeat

    assert sender.waited_ok is False
