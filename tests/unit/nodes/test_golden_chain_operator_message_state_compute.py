# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator arrivals reach the discovered message-state node and its typed terminal event."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from importlib.resources import files
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.model_operator_message_state import (
    ModelOperatorMessageStateReport,
    ModelOperatorMessageStateRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("include_reply", [False, True])
async def test_golden_chain_arrival_reply_and_replay(include_reply: bool) -> None:
    node_name = "node_operator_message_state_compute"
    contract = yaml.safe_load(
        files("omnimarket.nodes." + node_name).joinpath("contract.yaml").read_text()
    )
    contracts = tuple(c for c in discover_contracts().contracts if c.name == node_name)
    assert len(contracts) == 1
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    arrival = "2026-11-04T12:00:00Z | STATUS | lane=session-a | kind=operator-message | capture=prompt-a | item=message-a | owner=orchestrator | arrived"
    second = "2026-11-04T12:10:00Z | STATUS | lane=session-a | kind=operator-message | capture=prompt-b | item=message-b | owner=orchestrator | arrived"
    reply = "2026-11-04T12:01:00Z | MSG | from=orchestrator | to=operator | answers=prompt-a | Here is the answer."
    handoff = "2026-11-04T12:20:00Z | MSG | from=orchestrator | to=landing-controller | answers=prompt-b | Handed over."
    future = "2026-11-05T12:00:00Z | MSG | from=orchestrator | to=operator | answers=prompt-b | Future answer."
    rows = (second, handoff, future, *((reply,) if include_reply else ()), arrival)
    request = ModelOperatorMessageStateRequest(
        rows=rows,
        now=datetime(2026, 11, 4, 14, tzinfo=UTC),
        config=contract["config"],
    )
    received: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="operator-message-golden-chain")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        await received.put(json.loads(message.value))

    try:
        await bus.subscribe(
            terminal_topic, group_id="message-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        wiring = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [result.outcome.value for result in wiring.results] == ["wired"]
        assert wiring.results[0].topics_subscribed == (command_topic,)
        engine.freeze()
        reports: list[ModelOperatorMessageStateReport] = []
        for replay_rows in (rows, (*rows, arrival)):
            correlation = uuid4()
            envelope = ModelEventEnvelope[object](
                payload=request.model_copy(update={"rows": replay_rows}).model_dump(
                    mode="json"
                ),
                correlation_id=correlation,
                event_type=command_topic,
            )
            await bus.publish(
                command_topic, None, envelope.model_dump_json().encode(), None
            )
            event = await asyncio.wait_for(received.get(), timeout=5)
            assert event["correlation_id"] == str(correlation)
            reports.append(
                ModelOperatorMessageStateReport.model_validate(event["payload"])
            )
        assert reports[0] == reports[1]
        first, unanswered = reports[0].items
        assert first.state == ("answered" if include_reply else "unanswered")
        assert first.owner_lane == "orchestrator"
        assert first.age_minutes == 120
        assert bool(first.evidence) == include_reply
        assert unanswered.state == "unanswered"
        assert unanswered.owner_lane == "orchestrator"
        assert unanswered.age_minutes == 110
        assert reports[0].unanswered == (
            ("message-b",) if include_reply else ("message-a", "message-b")
        )
    finally:
        await bus.close()
