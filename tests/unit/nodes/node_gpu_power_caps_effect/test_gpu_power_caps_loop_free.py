# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loop-freedom of the GPU power caps node's request (OMN-24359 child).

With the lab_work backend the handler's send blocks until the host's receipt, so the request must run it off the runtime's
event loop; a blocking send on the loop thread is the failing control that shows the detector catches the regression.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsOutcome,
    ModelGpuPowerCapsRequest,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import SIM_A
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_lab_work import (
    lab_work_overlay,
    ok_line,
    receipt,
)

pytestmark = pytest.mark.unit


class _BlockingSender:
    """A sender whose send blocks on a release event, recording whether it was released in time."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.waited_ok = False
        self.calls: list[ModelLabWorkUnitRequest] = []

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.calls.append(request)
        self.waited_ok = self.released.wait(timeout=2.0)
        return receipt(EnumLabWorkUnitStatus.COMPLETED, tail=ok_line())


async def _release_after_a_turn(sender: _BlockingSender) -> None:
    await asyncio.sleep(0)
    sender.released.set()


def _request() -> ModelGpuPowerCapsRequest:
    return ModelGpuPowerCapsRequest(host=SIM_A, requested_by="lane-a")


async def test_the_request_leaves_the_event_loop_free_while_the_send_blocks() -> None:
    sender = _BlockingSender()
    handler = HandlerGpuPowerCaps(overlay=lab_work_overlay(), sender=sender)

    task = asyncio.create_task(handler.handle(_request()))
    await asyncio.sleep(0.05)
    sender.released.set()
    result = await task

    assert sender.waited_ok is True
    assert result.outcome is EnumGpuPowerCapsOutcome.OK
    assert len(sender.calls) == 1


async def test_control_a_send_run_on_the_loop_thread_starves_the_loop() -> None:
    sender = _BlockingSender()
    unit = ModelLabWorkUnitRequest(
        work_unit_id="gpc-loop-free-test",
        target_host=SIM_A,
        repo="o/r",
        commit_sha="0" * 40,
        argv=["true"],
        kind="other",
        timeout_seconds=1,
        lane="lane-a",
    )

    heartbeat = asyncio.create_task(_release_after_a_turn(sender))
    sender.send(unit)
    await heartbeat

    assert sender.waited_ok is False
