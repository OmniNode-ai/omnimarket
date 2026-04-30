"""Tests for the Pattern B broker publish adapter."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_publish import (
    AdapterPatternBBrokerPublish,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerOriginator,
    EnumPatternBBrokerRecipient,
    EnumPatternBBrokerState,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerPublishReceipt,
    ModelPatternBBrokerRuntimeConfig,
    ModelPatternBBrokerTopicBindings,
)

_BROKER_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "contract.yaml"
)
_PUBLISH_ADAPTER = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "handlers"
    / "adapter_broker_publish.py"
)


@pytest.mark.asyncio
async def test_publish_adapter_publishes_typed_request_to_contract_topic() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        adapter = AdapterPatternBBrokerPublish(event_bus=bus, config=config)
        request = ModelPatternBBrokerDispatchRequest(
            correlation_id=uuid4(),
            originator="omnimarket",
            recipient="omniclaude",
            skill_name="session-orchestrator",
            payload={"ticket_id": "OMN-10439"},
        )

        receipt = await adapter.publish(request)

        assert receipt.event_type is EnumPatternBBrokerEventType.dispatch_published
        assert receipt.state is EnumPatternBBrokerState.published
        assert receipt.topic == config.topics.dispatch_request_topic
        assert receipt.key == str(request.request_id)
        assert receipt.payload_size_bytes > 0

        history = await bus.get_event_history(
            topic=config.topics.dispatch_request_topic
        )
        assert len(history) == 1
        message = history[0]
        assert message.key == str(request.request_id).encode()

        payload = json.loads(message.value.decode())
        assert payload["request_id"] == str(request.request_id)
        assert payload["correlation_id"] == str(request.correlation_id)
        assert payload["event_type"] == "dispatch_requested"
        assert payload["state"] == "accepted"
        assert payload["originator"] == "omnimarket"
        assert payload["recipient"] == "omniclaude"
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_publish_adapter_rejects_originators_not_allowed_by_config() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        default_config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        config = ModelPatternBBrokerRuntimeConfig(
            topics=default_config.topics,
            consumer_group=default_config.consumer_group,
            default_wait_policy=default_config.default_wait_policy,
            allowed_originators=(EnumPatternBBrokerOriginator.omnicodex,),
            allowed_recipients=default_config.allowed_recipients,
        )
        adapter = AdapterPatternBBrokerPublish(event_bus=bus, config=config)
        request = ModelPatternBBrokerDispatchRequest(
            correlation_id=uuid4(),
            originator=EnumPatternBBrokerOriginator.omnimarket,
            recipient=EnumPatternBBrokerRecipient.omniclaude,
            skill_name="session-orchestrator",
        )

        with pytest.raises(PermissionError, match="originator"):
            await adapter.publish(request)

        history = await bus.get_event_history(
            topic=config.topics.dispatch_request_topic
        )
        assert history == []
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_publish_adapter_uses_injected_contract_topics() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        default_config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        topic_bindings = ModelPatternBBrokerTopicBindings(
            dispatch_request_topic="unit.fixture.delegate-task",
            terminal_completed_topic=default_config.topics.terminal_completed_topic,
            terminal_failed_topic=default_config.topics.terminal_failed_topic,
        )
        config = ModelPatternBBrokerRuntimeConfig(
            topics=topic_bindings,
            consumer_group=default_config.consumer_group,
            default_wait_policy=default_config.default_wait_policy,
            allowed_originators=default_config.allowed_originators,
            allowed_recipients=default_config.allowed_recipients,
        )
        adapter = AdapterPatternBBrokerPublish(event_bus=bus, config=config)
        request = ModelPatternBBrokerDispatchRequest(
            correlation_id=uuid4(),
            originator=EnumPatternBBrokerOriginator.omnimarket,
            recipient=EnumPatternBBrokerRecipient.omniclaude,
            skill_name="session-orchestrator",
        )

        receipt = await adapter.publish(request)

        assert receipt.topic == topic_bindings.dispatch_request_topic
        history = await bus.get_event_history(
            topic=topic_bindings.dispatch_request_topic
        )
        assert len(history) == 1
    finally:
        await bus.close()


@pytest.mark.unit
def test_publish_receipt_requires_published_event_and_state() -> None:
    with pytest.raises(ValidationError, match="dispatch_published"):
        ModelPatternBBrokerPublishReceipt(
            request_id=uuid4(),
            correlation_id=uuid4(),
            event_type=EnumPatternBBrokerEventType.dispatch_requested,
            topic="unit.fixture.delegate-task",
            key=str(uuid4()),
            payload_size_bytes=1,
            wait_policy=load_pattern_b_broker_config(
                _BROKER_CONTRACT
            ).default_wait_policy,
        )


@pytest.mark.unit
def test_publish_adapter_does_not_own_topic_literals_or_agent_imports() -> None:
    source = _PUBLISH_ADAPTER.read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
    assert "Agent(" not in source
    assert "PatternBrokerClient" not in source
