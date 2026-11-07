# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the host power effect (OMN-22906), on the in-memory bus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_power_effect.handlers.handler_host_power import (
    HandlerHostPower,
    select_backend,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerOutcome,
    EnumHostPowerRefusal,
    ModelHostPowerRequest,
    ModelHostPowerResult,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    ModelHostPowerOverlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src/omnimarket/nodes/node_host_power_effect"
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
CMD = CONTRACT["event_bus"]["subscribe_topics"][0]
EVT = CONTRACT["event_bus"]["publish_topics"][0]


async def chain(request_json: bytes) -> list[ModelHostPowerResult]:
    """Publish one command; the node consumes it and publishes its result; read the result."""
    overlay = ModelHostPowerOverlay.model_validate(
        yaml.safe_load(
            (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
        )
    )
    handler = HandlerHostPower(overlay, select_backend(overlay))
    bus = EventBusInmemory()
    await bus.start()
    seen: list[ModelHostPowerResult] = []

    async def node(msg: Any) -> None:
        request = ModelHostPowerRequest.model_validate_json(msg.value)
        result = handler.handle(request)
        await bus.publish(EVT, key=None, value=result.model_dump_json().encode())

    async def floor(msg: Any) -> None:
        seen.append(ModelHostPowerResult.model_validate_json(msg.value))

    await bus.subscribe(CMD, on_message=node, group_id="host-power-node")
    await bus.subscribe(EVT, on_message=floor, group_id="host-power-reader")
    await bus.publish(CMD, key=None, value=request_json)
    await bus.close()
    return seen


def test_host_power_topics_follow_the_naming_rule_and_the_terminal_event() -> None:
    assert CMD.startswith("onex.cmd.")
    assert CMD.endswith(".v1")
    assert EVT.startswith("onex.evt.")
    assert EVT.endswith(".v1")
    assert CONTRACT["terminal_event"] == EVT


async def test_host_power_golden_chain_a_command_yields_one_typed_result_event() -> (
    None
):
    request = ModelHostPowerRequest(
        host="sim-host-a",
        action="force_off",
        requested_by="lane-a",
        approval_ref="2026-11-02T11:00:00Z",
    )
    seen = await chain(request.model_dump_json().encode())
    assert len(seen) == 1
    assert seen[0].outcome is EnumHostPowerOutcome.OK
    assert seen[0].state_after.value == "off"


async def test_host_power_error_chain_a_host_with_no_route_yields_a_typed_refusal_event() -> (
    None
):
    request = ModelHostPowerRequest(
        host="sim-host-no-route", action="status", requested_by="lane-a"
    )
    seen = await chain(request.model_dump_json().encode())
    assert len(seen) == 1
    assert seen[0].outcome is EnumHostPowerOutcome.REFUSED
    assert seen[0].refusal is EnumHostPowerRefusal.NO_POWER_ROUTE


async def test_host_power_error_chain_reading_has_teeth_with_the_result_event_dropped() -> (
    None
):
    """Injected break: the node consumes the command and publishes nothing. The reading must see it."""
    bus = EventBusInmemory()
    await bus.start()
    seen: list[object] = []

    async def floor(msg: Any) -> None:
        seen.append(msg)

    async def silent_node(msg: Any) -> None:
        return None

    await bus.subscribe(CMD, on_message=silent_node, group_id="host-power-node")
    await bus.subscribe(EVT, on_message=floor, group_id="host-power-reader")
    await bus.publish(
        CMD,
        key=None,
        value=json.dumps(
            {"host": "sim-host-no-route", "action": "status", "requested_by": "lane-a"}
        ).encode(),
    )
    await bus.close()
    with pytest.raises(AssertionError):
        assert len(seen) == 1
