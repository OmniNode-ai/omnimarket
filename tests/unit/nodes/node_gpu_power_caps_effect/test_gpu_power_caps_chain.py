# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the GPU power caps effect (OMN-24042), on the in-memory bus."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsCardAction,
    EnumGpuPowerCapsOutcome,
    EnumGpuPowerCapsRefusal,
    ModelGpuPowerCapsRequest,
    ModelGpuPowerCapsResult,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import (
    NODE_DIR,
    SIM_A,
    build,
    caps_document,
    values_of,
)

pytestmark = pytest.mark.unit

CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
CMD = CONTRACT["event_bus"]["subscribe_topics"][0]
EVT = CONTRACT["event_bus"]["publish_topics"][0]


async def chain(
    handler: HandlerGpuPowerCaps,
    request: ModelGpuPowerCapsRequest,
    *,
    drop_result: bool = False,
) -> list[ModelGpuPowerCapsResult]:
    bus = EventBusInmemory()
    await bus.start()
    seen: list[ModelGpuPowerCapsResult] = []

    async def node(msg: Any) -> None:
        result = await handler.handle(
            ModelGpuPowerCapsRequest.model_validate_json(msg.value)
        )
        if not drop_result:
            await bus.publish(EVT, key=None, value=result.model_dump_json().encode())

    async def floor(msg: Any) -> None:
        seen.append(ModelGpuPowerCapsResult.model_validate_json(msg.value))

    await bus.subscribe(CMD, on_message=node, group_id="gpu-power-caps-node")
    await bus.subscribe(EVT, on_message=floor, group_id="gpu-power-caps-reader")
    await bus.publish(CMD, key=None, value=request.model_dump_json().encode())
    await bus.close()
    return seen


def request() -> ModelGpuPowerCapsRequest:
    return ModelGpuPowerCapsRequest(host=SIM_A, requested_by="lane-a")


def test_topics_follow_the_naming_rule_and_the_terminal_event() -> None:
    assert CMD.startswith("onex.cmd.")
    assert CMD.endswith(".v1")
    assert EVT.startswith("onex.evt.")
    assert EVT.endswith(".v1")
    assert EVT == "onex.evt.omnimarket.gpu-power-caps-completed.v1"
    assert CONTRACT["terminal_event"] == EVT


async def test_golden_chain_a_command_yields_one_typed_result_event() -> None:
    handler, _ = build(caps_document())
    seen = await chain(handler, request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.OK
    assert [r.action for r in seen[0].cards] == [EnumGpuPowerCapsCardAction.APPLIED] * 2
    assert [r.after_watts for r in seen[0].cards] == [500, 300]


async def test_error_chain_a_wrong_card_name_yields_a_typed_refusal_event() -> None:
    doc = caps_document()
    values_of(doc, "gpu-power-caps-sim-a")["GPU_NAME_0"] = "No Such Card"
    handler, backend = build(doc)
    seen = await chain(handler, request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.REFUSED
    assert seen[0].refusal is EnumGpuPowerCapsRefusal.CARD_NOT_FOUND
    assert backend.set_calls == []


async def test_error_chain_a_failed_write_yields_a_failed_event() -> None:
    handler, backend = build(caps_document())
    backend.failing_indexes.add((SIM_A, 0))
    seen = await chain(handler, request())
    assert len(seen) == 1
    assert seen[0].outcome is EnumGpuPowerCapsOutcome.FAILED


async def test_error_chain_reading_has_teeth_with_the_result_event_dropped() -> None:
    """Injected break: the node consumes the command and publishes no result. The reading must see it."""
    handler, _ = build(caps_document())
    seen = await chain(handler, request(), drop_result=True)
    with pytest.raises(AssertionError):
        assert len(seen) == 1
