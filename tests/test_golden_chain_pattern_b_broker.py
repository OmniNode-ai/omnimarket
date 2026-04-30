"""Golden-chain proof for the Pattern B broker publish-to-terminal flow."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pattern_b_broker.handlers import (
    AdapterPatternBBrokerPublish,
    AdapterPatternBBrokerTerminalConsumer,
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerState,
    EnumPatternBBrokerTerminalStatus,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerTerminalEvent,
)


@pytest.mark.asyncio
async def test_pattern_b_broker_publish_to_terminal_flow() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config()
        publisher = AdapterPatternBBrokerPublish(event_bus=bus, config=config)
        terminal_consumer = AdapterPatternBBrokerTerminalConsumer(
            event_bus=bus,
            config=config,
        )
        request = ModelPatternBBrokerDispatchRequest(
            correlation_id=uuid4(),
            originator="omnimarket",
            recipient="omniclaude",
            skill_name="session-orchestrator",
            payload={"ticket_id": "OMN-10442"},
        )
        wait_task = asyncio.create_task(
            terminal_consumer.wait_for_terminal_event(request, timeout_seconds=1.0)
        )
        await asyncio.sleep(0)

        receipt = await publisher.publish(request)
        terminal_event = ModelPatternBBrokerTerminalEvent(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            event_type=EnumPatternBBrokerEventType.terminal_completed,
            state=EnumPatternBBrokerState.completed,
            status=EnumPatternBBrokerTerminalStatus.completed,
            result={"proof": "complete"},
        )
        await bus.publish(
            topic=config.topics.terminal_completed_topic,
            key=str(request.request_id).encode(),
            value=terminal_event.model_dump_json().encode(),
        )
        observed_terminal = await wait_task

        assert receipt.request_id == request.request_id
        assert receipt.correlation_id == request.correlation_id
        assert observed_terminal.request_id == request.request_id
        assert observed_terminal.status is EnumPatternBBrokerTerminalStatus.completed
        dispatch_history = await bus.get_event_history(
            topic=config.topics.dispatch_request_topic
        )
        terminal_history = await bus.get_event_history(
            topic=config.topics.terminal_completed_topic
        )
        assert len(dispatch_history) == 1
        assert len(terminal_history) == 1
    finally:
        await bus.close()
