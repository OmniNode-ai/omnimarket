# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the unit probe effect on the in-memory bus: a demand on the contract's command topic is handled and
its typed result is published on the contract's terminal topic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_unit_probe_effect.handlers.handler_unit_probe import (
    HandlerUnitProbe,
)
from omnimarket.nodes.node_unit_probe_effect.models.model_unit_probe import (
    ModelUnitProbeRequest,
    ModelUnitProbeResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_unit_probe_effect"
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
CMD = CONTRACT["event_bus"]["subscribe_topics"][0]
EVT = CONTRACT["event_bus"]["publish_topics"][0]


def test_contract_declares_the_demand_and_result_topics() -> None:
    assert CMD == "onex.cmd.omnimarket.unit-probe-requested.v1"
    assert EVT == "onex.evt.omnimarket.unit-probe-completed.v1"
    assert CONTRACT["runtime_dispatch"]["command_topic"] == CMD
    assert CONTRACT["terminal_event"] == EVT
    assert CONTRACT["event_bus"]["subscribe_topics"] == [CMD]
    assert CONTRACT["event_bus"]["publish_topics"] == [EVT]
    assert CONTRACT["runtime_profiles"] == ["effects"]
    routed = CONTRACT["handler_routing"]["handlers"][0]["event_model"]["name"]
    assert routed == "ModelUnitProbeRequest"


@pytest.mark.asyncio
async def test_a_demand_is_handled_and_its_result_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = HandlerUnitProbe()
    # A host with no systemctl and an unreadable pool: the unit's state is unknown, never unloaded.

    def no_systemctl(_self: HandlerUnitProbe, _request: ModelUnitProbeRequest) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(HandlerUnitProbe, "local", no_systemctl)
    bus = EventBusInmemory()
    await bus.start()
    seen: list[ModelUnitProbeResult] = []

    async def node(msg: Any) -> None:
        result = handler.handle(ModelUnitProbeRequest.model_validate_json(msg.value))
        await bus.publish(EVT, key=None, value=result.model_dump_json().encode())

    async def reader(msg: Any) -> None:
        seen.append(ModelUnitProbeResult.model_validate_json(msg.value))

    await bus.subscribe(CMD, on_message=node, group_id="unit-probe-node")
    await bus.subscribe(EVT, on_message=reader, group_id="unit-probe-reader")
    request = ModelUnitProbeRequest(
        manager="systemd",
        label="",
        name="onex-lane-census.timer",
        out_paths=(),
        landing_placement_path="/nonexistent/landing_placement.py",
    )
    await bus.publish(CMD, key=None, value=request.model_dump_json().encode())
    assert seen == [ModelUnitProbeResult(loaded=None, last_run=None, failure="")]
    await bus.close()
