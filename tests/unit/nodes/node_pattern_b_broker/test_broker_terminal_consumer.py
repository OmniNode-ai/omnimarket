"""Tests for the Pattern B broker terminal-event consumer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_terminal_consumer import (
    AdapterPatternBBrokerTerminalConsumer,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerState,
    EnumPatternBBrokerTerminalStatus,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerTerminalEvent,
)

_BROKER_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "contract.yaml"
)
_TERMINAL_CONSUMER = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "handlers"
    / "adapter_broker_terminal_consumer.py"
)


@pytest.mark.asyncio
async def test_wait_returns_correlated_completed_event() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        adapter = AdapterPatternBBrokerTerminalConsumer(event_bus=bus, config=config)
        request = _make_request()
        wait_task = asyncio.create_task(
            adapter.wait_for_terminal_event(request, timeout_seconds=0.5)
        )

        await asyncio.sleep(0)
        completed = ModelPatternBBrokerTerminalEvent(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            event_type=EnumPatternBBrokerEventType.terminal_completed,
            state=EnumPatternBBrokerState.completed,
            status=EnumPatternBBrokerTerminalStatus.completed,
            result={"summary": "done"},
        )
        await bus.publish(
            topic=config.topics.terminal_completed_topic,
            key=str(request.request_id).encode(),
            value=completed.model_dump_json().encode(),
        )

        result = await wait_task

        assert result.event_type is EnumPatternBBrokerEventType.terminal_completed
        assert result.state is EnumPatternBBrokerState.completed
        assert result.status is EnumPatternBBrokerTerminalStatus.completed
        assert result.result["summary"] == "done"
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_wait_ignores_unrelated_events_and_returns_matching_failed_event() -> (
    None
):
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        adapter = AdapterPatternBBrokerTerminalConsumer(event_bus=bus, config=config)
        request = _make_request()
        wait_task = asyncio.create_task(
            adapter.wait_for_terminal_event(request, timeout_seconds=0.5)
        )

        await asyncio.sleep(0)
        unrelated = ModelPatternBBrokerTerminalEvent(
            request_id=uuid4(),
            correlation_id=request.correlation_id,
            event_type=EnumPatternBBrokerEventType.terminal_completed,
            state=EnumPatternBBrokerState.completed,
            status=EnumPatternBBrokerTerminalStatus.completed,
        )
        await bus.publish(
            topic=config.topics.terminal_completed_topic,
            key=b"unrelated",
            value=unrelated.model_dump_json().encode(),
        )

        failed = ModelPatternBBrokerTerminalEvent(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            event_type=EnumPatternBBrokerEventType.terminal_failed,
            state=EnumPatternBBrokerState.failed,
            status=EnumPatternBBrokerTerminalStatus.failed,
            error_message="delegate failed",
        )
        await bus.publish(
            topic=config.topics.terminal_failed_topic,
            key=str(request.request_id).encode(),
            value=failed.model_dump_json().encode(),
        )

        result = await wait_task

        assert result.event_type is EnumPatternBBrokerEventType.terminal_failed
        assert result.state is EnumPatternBBrokerState.failed
        assert result.status is EnumPatternBBrokerTerminalStatus.failed
        assert result.error_message == "delegate failed"
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_wait_returns_typed_timeout_event() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        adapter = AdapterPatternBBrokerTerminalConsumer(event_bus=bus, config=config)
        request = _make_request()

        result = await adapter.wait_for_terminal_event(request, timeout_seconds=0.01)

        assert result.request_id == request.request_id
        assert result.correlation_id == request.correlation_id
        assert result.event_type is EnumPatternBBrokerEventType.terminal_timed_out
        assert result.state is EnumPatternBBrokerState.timed_out
        assert result.status is EnumPatternBBrokerTerminalStatus.timed_out
        assert result.error_message is not None
        assert "timed out" in result.error_message
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_wait_cleans_first_subscription_when_second_subscribe_fails() -> None:
    config = load_pattern_b_broker_config(_BROKER_CONTRACT)
    bus = _FailingSecondSubscribeBus()
    adapter = AdapterPatternBBrokerTerminalConsumer(event_bus=bus, config=config)

    with pytest.raises(RuntimeError, match="second subscribe failed"):
        await adapter.wait_for_terminal_event(_make_request(), timeout_seconds=0.01)

    assert bus.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_wait_attempts_all_unsubscribers_when_cleanup_raises() -> None:
    config = load_pattern_b_broker_config(_BROKER_CONTRACT)
    bus = _RaisingFirstUnsubscribeBus()
    adapter = AdapterPatternBBrokerTerminalConsumer(event_bus=bus, config=config)

    result = await adapter.wait_for_terminal_event(
        _make_request(), timeout_seconds=0.01
    )

    assert result.event_type is EnumPatternBBrokerEventType.terminal_timed_out
    assert bus.unsubscribe_calls == 2


@pytest.mark.unit
def test_terminal_consumer_does_not_own_topic_literals_or_client_names() -> None:
    source = _TERMINAL_CONSUMER.read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
    assert "PatternBrokerClient" not in source


def _make_request() -> ModelPatternBBrokerDispatchRequest:
    return ModelPatternBBrokerDispatchRequest(
        correlation_id=uuid4(),
        originator="omnimarket",
        recipient="omniclaude",
        skill_name="session-orchestrator",
    )


class _FailingSecondSubscribeBus:
    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.unsubscribe_calls = 0

    async def subscribe(self, *args: object, **kwargs: object):
        self.subscribe_calls += 1
        if self.subscribe_calls == 2:
            raise RuntimeError("second subscribe failed")

        async def unsubscribe() -> None:
            self.unsubscribe_calls += 1

        return unsubscribe


class _RaisingFirstUnsubscribeBus:
    def __init__(self) -> None:
        self.unsubscribe_calls = 0

    async def subscribe(self, *args: object, **kwargs: object):
        call_number = self.unsubscribe_calls

        async def unsubscribe() -> None:
            self.unsubscribe_calls += 1
            if call_number == 0:
                raise RuntimeError("first unsubscribe failed")

        return unsubscribe
