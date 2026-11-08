# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error join tests for each topic consumed with no producer found (OMN-23151 AC2 and AC3).

Each topic of the committed no-producer list is a join: a message enters the bus under a probe topic, the join gate in
front of the topic's consumer decides it, and the decision comes back over the bus.

* golden join: an event enters, the consumer starts exactly once on that payload, and the chain ends with a started
  decision;
* error join: the producer side's typed failure enters, the consumer receives it, does not start, and the chain ends with
  a refusal that names ``upstream_<code>``. The same assertion is run against a consumer that ignores the failure and
  starts anyway, and it must fail there.

The in-memory leg runs everywhere. The dev-bus leg uses the declared test lane's broker through ``kafka_integration_bus``
and is selected with ``-m kafka`` (the merge gate runs ``-m "not kafka"``). Probe topics carry a unique run id under the
contract's probe prefix; no topic of the system is written.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_seam_reading_compute.adapters.inmemory_consumer_start import (
    InMemoryConsumerStart,
)
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_no_producer_join import (
    HandlerNoProducerJoin,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_no_producer_topic import (
    EnumJoinOutcome,
    ModelNoProducerJoinRequest,
    ModelNoProducerJoinResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_seam_reading_compute"
)
NOW = datetime(2026, 11, 8, 22, 45, tzinfo=UTC)
TIMEOUT_S = 30.0
CONSUMER_JOIN_S = 3.0
FAILURE_CODE = "no_producer_reachable"
PROBE_PREFIX = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))[
    "topics"
]["no_producer_join_probe_prefix"]
ENTRIES: list[dict[str, Any]] = yaml.safe_load(
    (NODE_DIR / "no_producer_topics.yaml").read_text(encoding="utf-8")
)["topics"]


def _consumer(entry: dict[str, Any]) -> str:
    """The consumer node of the topic's first consumer contract: its directory name."""
    return Path(entry["consumers"][0]).parent.name


class ProtocolGate(Protocol):
    def handle(
        self, request: ModelNoProducerJoinRequest
    ) -> ModelNoProducerJoinResult: ...


class IgnoringGate:
    """A consumer that ignores the typed failure: it starts on whatever arrives."""

    def __init__(self, start: InMemoryConsumerStart) -> None:
        self._start = start

    def handle(self, request: ModelNoProducerJoinRequest) -> ModelNoProducerJoinResult:
        self._start.start(request.consumer, request.topic, request.payload)
        return ModelNoProducerJoinResult(
            topic=request.topic,
            consumer=request.consumer,
            outcome=EnumJoinOutcome.STARTED,
            consumer_started=True,
            decided_at=request.now,
        )


def _event_payload(entry: dict[str, Any]) -> str:
    return json.dumps({"event": "arrived", "topic": entry["topic"]}, sort_keys=True)


def _failure_payload(entry: dict[str, Any]) -> str:
    return json.dumps(
        {
            "kind": "typed_failure",
            "topic": entry["topic"],
            "failure_code": FAILURE_CODE,
            "detail": "the producer side failed before it sent",
        },
        sort_keys=True,
    )


async def run_join(
    bus: Any,
    entry: dict[str, Any],
    payload: str,
    make_gate: Callable[[InMemoryConsumerStart], ProtocolGate],
    *,
    consume: bool = False,
) -> tuple[ModelNoProducerJoinResult, InMemoryConsumerStart]:
    """Send one message through the bus to the gate in front of the consumer and read its decision back."""
    run = uuid.uuid4().hex[:12]
    inbound = f"{PROBE_PREFIX}-{run}-in.v1"
    outbound = f"{PROBE_PREFIX}-{run}-out.v1"
    starts = InMemoryConsumerStart()
    gate = make_gate(starts)
    answered: asyncio.Future[ModelNoProducerJoinResult] = (
        asyncio.get_running_loop().create_future()
    )

    async def serve(message: Any) -> None:
        body = json.loads(message.value)
        result = gate.handle(
            ModelNoProducerJoinRequest(
                topic=body["topic"],
                consumer=body["consumer"],
                payload=body["payload"],
                now=NOW,
            )
        )
        await bus.publish(outbound, None, result.model_dump_json().encode())

    async def answer(message: Any) -> None:
        if not answered.done():
            answered.set_result(
                ModelNoProducerJoinResult.model_validate_json(message.value)
            )

    unsubscribe_in = await bus.subscribe(
        inbound, on_message=serve, group_id=f"in-{run}"
    )
    unsubscribe_out = await bus.subscribe(
        outbound, on_message=answer, group_id=f"out-{run}"
    )
    consuming: asyncio.Task[None] | None = None
    try:
        if consume:
            # A broker bus attaches its consumers in start_consuming, which runs until shutdown.
            consuming = asyncio.create_task(bus.start_consuming())
            await asyncio.sleep(CONSUMER_JOIN_S)
        message = {
            "topic": entry["topic"],
            "consumer": _consumer(entry),
            "payload": payload,
        }
        await bus.publish(inbound, None, json.dumps(message).encode())
        return await asyncio.wait_for(answered, TIMEOUT_S), starts
    finally:
        if consuming is not None:
            consuming.cancel()
        await unsubscribe_in()
        await unsubscribe_out()


def assert_golden_join(
    entry: dict[str, Any],
    payload: str,
    result: ModelNoProducerJoinResult,
    starts: InMemoryConsumerStart,
) -> None:
    assert result.outcome is EnumJoinOutcome.STARTED
    assert result.consumer_started is True
    assert result.failure_code is None
    assert starts.starts == [(_consumer(entry), entry["topic"], payload)]


def assert_error_join(
    entry: dict[str, Any],
    result: ModelNoProducerJoinResult,
    starts: InMemoryConsumerStart,
) -> None:
    assert result.outcome is EnumJoinOutcome.REFUSED
    assert result.consumer_started is False
    assert result.failure_code == f"upstream_{FAILURE_CODE}"
    assert result.consumer == _consumer(entry)
    assert starts.starts == []


async def _inmemory(
    body: Callable[[Any], Awaitable[None]],
) -> None:
    bus = EventBusInmemory(environment="no-producer-join", group="no-producer-join")
    await bus.start()
    try:
        await body(bus)
    finally:
        await bus.close()


def _gate(start: InMemoryConsumerStart) -> ProtocolGate:
    return HandlerNoProducerJoin(start=start)


def test_no_producer_topic_join_list_is_not_empty_and_names_each_consumer() -> None:
    assert ENTRIES
    assert all(_consumer(e).startswith("node_") for e in ENTRIES)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["topic"])
async def test_no_producer_topic_golden_join_inmemory_bus(
    entry: dict[str, Any],
) -> None:
    async def body(bus: Any) -> None:
        payload = _event_payload(entry)
        result, starts = await run_join(bus, entry, payload, _gate)
        assert_golden_join(entry, payload, result, starts)

    await _inmemory(body)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["topic"])
async def test_no_producer_topic_error_join_inmemory_bus(entry: dict[str, Any]) -> None:
    async def body(bus: Any) -> None:
        result, starts = await run_join(bus, entry, _failure_payload(entry), _gate)
        assert_error_join(entry, result, starts)

    await _inmemory(body)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["topic"])
async def test_no_producer_topic_error_join_is_red_against_a_consumer_that_ignores_it(
    entry: dict[str, Any],
) -> None:
    async def body(bus: Any) -> None:
        result, starts = await run_join(
            bus, entry, _failure_payload(entry), lambda s: IgnoringGate(s)
        )
        assert starts.starts, "the ignoring consumer must have started"
        with pytest.raises(AssertionError):
            assert_error_join(entry, result, starts)

    await _inmemory(body)


@pytest.mark.parametrize("bad", ["not json at all", '{"kind": "typed_failure"}'])
async def test_no_producer_topic_error_join_unreadable_message_is_refused_not_started(
    bad: str,
) -> None:
    entry = ENTRIES[0]

    async def body(bus: Any) -> None:
        result, starts = await run_join(bus, entry, bad, _gate)
        assert result.outcome is EnumJoinOutcome.REFUSED
        assert result.failure_code == "bad-payload"
        assert starts.starts == []

    await _inmemory(body)


async def test_no_producer_topic_error_join_failure_for_another_topic_is_refused() -> (
    None
):
    entry, other = ENTRIES[0], ENTRIES[1]

    async def body(bus: Any) -> None:
        result, starts = await run_join(bus, entry, _failure_payload(other), _gate)
        assert result.outcome is EnumJoinOutcome.REFUSED
        assert result.failure_code == "topic-mismatch"
        assert starts.starts == []

    await _inmemory(body)


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["topic"])
async def test_no_producer_topic_golden_join_dev_bus(
    entry: dict[str, Any], kafka_integration_bus: Any
) -> None:
    payload = _event_payload(entry)
    result, starts = await run_join(
        kafka_integration_bus, entry, payload, _gate, consume=True
    )
    assert_golden_join(entry, payload, result, starts)


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["topic"])
async def test_no_producer_topic_error_join_dev_bus(
    entry: dict[str, Any], kafka_integration_bus: Any
) -> None:
    result, starts = await run_join(
        kafka_integration_bus, entry, _failure_payload(entry), _gate, consume=True
    )
    assert_error_join(entry, result, starts)
