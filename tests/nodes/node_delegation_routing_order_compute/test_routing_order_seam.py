# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the routing-order node's command-in, answer-out chain over the runtime wiring (OMN-23864).

The deployed runtime discovers the installed contract, wires its handler with
``wire_from_manifest`` and subscribes the contract's command topic. These cases
drive that path on the in-memory bus with the routing overlay bound by
``DELEGATION_ROUTING_OVERLAY_PATH``. The real-broker leg is defined only when
``RUN_REDPANDA_ROUTING_ORDER=1`` (a backend asked for and unreachable fails;
nothing here skips), on disposable topics of the declared test bus lane.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from omnibase_core.enums.enum_delegation_routing_order_refusal import (
    EnumDelegationRoutingOrderRefusal,
)
from omnibase_core.models.delegation.model_delegation_routing_order_answer import (
    ModelDelegationRoutingOrderAnswer,
)
from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

import omnimarket.nodes.node_delegation_routing_order_compute as node_package
from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from tests.nodes.node_delegation_routing_order_compute.test_order_and_attempts_read import (
    _overlay_dict,
    _write_overlay,
)

pytestmark = pytest.mark.unit

REDPANDA_FLAG = "RUN_REDPANDA_ROUTING_ORDER"

NODE = "node_delegation_routing_order_compute"
NODE_DIR = Path(node_package.__file__).parent
EVENT_TYPE = "omnimarket.delegation-routing-order-requested"
WAIT_SECONDS = 5


def _contract_topics() -> tuple[str, str]:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return contract["runtime_dispatch"]["command_topic"], contract["terminal_event"]


COMMAND_TOPIC, RESOLVED_TOPIC = _contract_topics()


class WiredBus:
    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.resolved: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def command(
        self, payload: dict[str, Any], *, topic: str = COMMAND_TOPIC
    ) -> uuid.UUID:
        correlation_id = uuid.uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload, correlation_id=correlation_id, event_type=EVENT_TYPE
        )
        await self.bus.publish(topic, None, envelope.model_dump_json().encode(), None)
        return correlation_id

    async def resolved_for(self, correlation_id: uuid.UUID) -> dict[str, Any]:
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                event = await self.resolved.get()
                if event["correlation_id"] == str(correlation_id):
                    return event


@pytest_asyncio.fixture
async def wired() -> AsyncIterator[WiredBus]:
    contracts = tuple(c for c in discover_contracts().contracts if c.name == NODE)
    assert len(contracts) == 1, f"{NODE} is not discovered from the installed package"
    bus = EventBusInmemory(environment="local", group="delegation-routing-order-wiring")
    await bus.start()
    harness = WiredBus(bus)

    async def collect(message: ModelEventMessage) -> None:
        await harness.resolved.put(json.loads(message.value))

    await bus.subscribe(
        RESOLVED_TOPIC, group_id="resolved-collector", on_message=collect
    )
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=contracts),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert {r.contract_name: r.outcome.value for r in report.results} == {NODE: "wired"}
    assert report.results[0].topics_subscribed == (COMMAND_TOPIC,)
    engine.freeze()
    try:
        yield harness
    finally:
        await bus.close()


def _command_payload() -> dict[str, Any]:
    return {
        "task_class": "code_generation",
        "tenant_slug": HOUSE_TENANT_SLUG,
        "surface": "internal",
        "pin_lifted": True,
    }


@pytest.mark.asyncio
async def test_golden_command_reaches_the_overlay_rungs(
    wired: WiredBus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    correlation_id = await wired.command(_command_payload())
    result = (await wired.resolved_for(correlation_id))["payload"]
    answer = ModelDelegationRoutingOrderAnswer.model_validate(result)
    assert answer.ok is True
    expected_rungs = _overlay_dict()["escalation_chains"][0]["rungs"]
    assert [rung.tier for rung in answer.rungs] == expected_rungs


@pytest.mark.asyncio
async def test_unbound_overlay_is_answered_refused(
    wired: WiredBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH", raising=False)
    correlation_id = await wired.command(_command_payload())
    result = (await wired.resolved_for(correlation_id))["payload"]
    answer = ModelDelegationRoutingOrderAnswer.model_validate(result)
    assert answer.ok is False
    assert answer.refusal == EnumDelegationRoutingOrderRefusal.OVERLAY_UNBOUND
    assert list(answer.rungs) == []


def test_answer_and_request_shapes_match_the_contract() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert set(ModelDelegationRoutingOrderAnswer.model_fields) == set(
        contract["outputs"]
    )
    assert set(ModelDelegationRoutingOrderRequest.model_fields) == set(
        contract["inputs"]
    )


@pytest.mark.asyncio
async def test_a_command_on_another_topic_gets_no_answer(wired: WiredBus) -> None:
    """Negative control: the harness reports silence when the node is not addressed."""
    correlation_id = await wired.command(
        _command_payload(), topic=COMMAND_TOPIC + ".elsewhere"
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await wired.resolved_for(correlation_id)


def test_topics_are_pinned_to_the_names_the_step_reads() -> None:
    assert COMMAND_TOPIC == "onex.cmd.omnimarket.delegation-routing-order-requested.v1"
    assert RESOLVED_TOPIC == "onex.evt.omnimarket.delegation-routing-order-read.v1"


if os.environ.get(REDPANDA_FLAG) == "1":

    @pytest.mark.kafka
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_routing_order_chain_over_redpanda(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command crosses the real broker, the handler answers, the answer crosses it back.

        Uses piece 16's broker leg (the declared test lane's own identity, a disposable topic tagged
        ``drill``, created and left, no delete); a lane that resolves no identity fails here, never skips.
        """
        from tests.chains.piece16.test_piece16_chain import RedpandaChainBus

        monkeypatch.setenv(
            "DELEGATION_ROUTING_OVERLAY_PATH", str(_write_overlay(tmp_path))
        )
        bus = RedpandaChainBus()
        request = ModelDelegationRoutingOrderRequest.model_validate(_command_payload())
        arrived = await bus.roundtrip(COMMAND_TOPIC, request.model_dump_json().encode())
        result = HandlerDelegationRoutingOrder().handle(
            ModelDelegationRoutingOrderRequest.model_validate_json(arrived)
        )
        done = await bus.roundtrip(RESOLVED_TOPIC, result.model_dump_json().encode())
        answer = ModelDelegationRoutingOrderAnswer.model_validate_json(done)
        assert answer.ok is True
        expected = _overlay_dict()["escalation_chains"][0]["rungs"]
        assert [rung.tier for rung in answer.rungs] == expected
