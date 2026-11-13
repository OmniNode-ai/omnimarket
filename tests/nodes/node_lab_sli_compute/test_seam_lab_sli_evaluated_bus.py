# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the lab SLI evaluated topic reaches its consumers through the runtime wiring.

``node_lab_sli_compute`` publishes ``ModelLabSliEvaluationBatch`` on
``onex.evt.omnimarket.lab-sli-evaluated.v1``. The board and alert nodes subscribe
that topic and their ``handler_routing`` entry now declares the batch as their
``event_model``. This test wires each consumer contract the way the kernel does
(``discover_contracts_from_paths`` -> ``wire_from_manifest`` on an
``EventBusInmemory``), records every handler call, publishes the producer's batch
the way the wire does, and asserts the handler ran exactly once with a
``ModelLabSliEvaluationBatch``-equal argument and that the node's terminal topic
received one published message. A failing control publishes a payload that is not
a batch and asserts no handler call reaches the handler (type scoping refuses it).

The alert effect is built with no event publisher, so its operator push is recorded
as a delivery error on the result and no secret is read; the board handler is pure.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.lab_sli import ModelLabSliEvaluationBatch
from omnimarket.nodes.node_lab_sli_alert_effect.handlers.handler_lab_sli_alert import (
    HandlerLabSliAlert,
)
from omnimarket.nodes.node_lab_sli_board_compute.handlers.handler_lab_sli_board import (
    HandlerLabSliBoard,
)
from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
)
from tests.nodes.node_lab_sli_compute.test_golden_chain_lab_sli import _night

pytestmark = pytest.mark.integration

_EVALUATED_TOPIC = "onex.evt.omnimarket.lab-sli-evaluated.v1"

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"

_CONSUMERS = {
    "node_lab_sli_board_compute": {
        "contract": _NODES / "node_lab_sli_board_compute" / "contract.yaml",
        "handler": HandlerLabSliBoard,
        "terminal": "onex.evt.omnimarket.lab-sli-board-rendered.v1",
    },
    "node_lab_sli_alert_effect": {
        "contract": _NODES / "node_lab_sli_alert_effect" / "contract.yaml",
        "handler": HandlerLabSliAlert,
        "terminal": "onex.evt.omnimarket.lab-sli-alerted.v1",
    },
}


def _producer_batch() -> ModelLabSliEvaluationBatch:
    return HandlerLabSliEvaluate().handle(_night())


def _envelope_bytes(payload: object) -> bytes:
    envelope = ModelEventEnvelope[object](
        payload=payload,
        correlation_id=uuid4(),
        source_tool="lab-sli-evaluated-bus-test",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _wired_consumer(
    monkeypatch: pytest.MonkeyPatch,
    consumer: dict[str, object],
) -> tuple[EventBusInmemory, list[object]]:
    """Wire one consumer contract as the kernel does; record every handler call."""
    handler_cls = consumer["handler"]
    handled: list[object] = []
    real_handle = handler_cls.handle

    def recording_handle(self: object, request: object) -> object:
        result = real_handle(self, request)
        handled.append(request)
        return result

    monkeypatch.setattr(handler_cls, "handle", recording_handle)
    manifest = discover_contracts_from_paths([consumer["contract"]])
    assert manifest.total_errors == 0
    bus = EventBusInmemory(environment="test", group="lab-sli-evaluated-bus")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="test"
    )
    assert report.total_wired == 1
    engine.freeze()
    return bus, handled


async def _published_payloads(
    bus: EventBusInmemory, topic: str
) -> list[dict[str, object]]:
    history = await bus.get_event_history(topic=topic)
    payloads: list[dict[str, object]] = []
    for message in history:
        payload = json.loads(message.value)["payload"]
        assert isinstance(payload, dict)
        payloads.append(payload)
    return payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer_name", list(_CONSUMERS), ids=list(_CONSUMERS))
async def test_evaluated_batch_reaches_consumer_and_terminal_topic(
    monkeypatch: pytest.MonkeyPatch, consumer_name: str
) -> None:
    consumer = _CONSUMERS[consumer_name]
    batch = _producer_batch()
    bus, handled = await _wired_consumer(monkeypatch, consumer)
    try:
        await bus.publish(
            _EVALUATED_TOPIC, None, _envelope_bytes(batch.model_dump(mode="json")), None
        )

        # Positive control: the handler ran exactly once with a batch-equal argument.
        assert len(handled) == 1
        assert ModelLabSliEvaluationBatch.model_validate(handled[0]) == batch

        # The node's terminal topic received one published message.
        payloads = await _published_payloads(bus, consumer["terminal"])
        assert len(payloads) == 1
    finally:
        await bus.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer_name", list(_CONSUMERS), ids=list(_CONSUMERS))
async def test_non_batch_payload_reaches_no_handler(
    monkeypatch: pytest.MonkeyPatch, consumer_name: str
) -> None:
    consumer = _CONSUMERS[consumer_name]
    bus, handled = await _wired_consumer(monkeypatch, consumer)
    try:
        await bus.publish(_EVALUATED_TOPIC, None, _envelope_bytes({"nope": 1}), None)
        # Type scoping refuses a payload that is not a ModelLabSliEvaluationBatch.
        assert handled == []
        assert await _published_payloads(bus, consumer["terminal"]) == []
    finally:
        await bus.close()
