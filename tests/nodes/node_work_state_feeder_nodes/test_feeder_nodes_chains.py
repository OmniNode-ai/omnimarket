# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three scheduled work-state feeders: their contracts, their chains, their bus (OMN-23427).

Seam: each feeder node subscribes the runtime tick, publishes exactly one terminal
``work-state-<s>-fed`` topic no other node claims, and answers a due tick with a
ModelWorkStateFeederEvent built by the shared handler from a fake feeder port. The
in-memory bus leg runs everywhere; the Redpanda leg runs against the plaintext lab
broker named in ``WORK_STATE_PARITY_BOOTSTRAP``:

    WORK_STATE_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "work_state_parity and kafka" -q tests/nodes/node_work_state_feeder_nodes
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.models.work_state_feeder import (
    EnumFeeder,
    EnumFeederProfile,
    ModelFeederOutcome,
    ModelFeederScheduleOverlay,
    ModelWorkStateFeederEvent,
)
from omnimarket.protocols.protocol_work_state_feeder import ProtocolWorkStateFeeder

pytestmark = [pytest.mark.unit, pytest.mark.work_state_parity]

BOOTSTRAP_ENV = "WORK_STATE_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
TICK = "onex.intent.platform.runtime-tick.v1"
T0 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"

CASES = [
    pytest.param(
        "node_work_state_linear_feeder_scheduled_effect",
        (
            "omnimarket.nodes.node_work_state_linear_feeder_scheduled_effect"
            ".handlers.handler_linear_feeder_scheduled"
        ),
        "HandlerWorkStateLinearFeederScheduled",
        EnumFeeder.LINEAR,
        "onex.evt.omnimarket.work-state-linear-fed.v1",
        "work_state.scheduled_feed_linear",
        id="linear",
    ),
    pytest.param(
        "node_work_state_readings_feeder_scheduled_effect",
        (
            "omnimarket.nodes.node_work_state_readings_feeder_scheduled_effect"
            ".handlers.handler_readings_feeder_scheduled"
        ),
        "HandlerWorkStateReadingsFeederScheduled",
        EnumFeeder.READINGS,
        "onex.evt.omnimarket.work-state-readings-fed.v1",
        "work_state.scheduled_feed_readings",
        id="readings",
    ),
    pytest.param(
        "node_work_state_receipts_feeder_scheduled_effect",
        (
            "omnimarket.nodes.node_work_state_receipts_feeder_scheduled_effect"
            ".handlers.handler_receipts_feeder_scheduled"
        ),
        "HandlerWorkStateReceiptsFeederScheduled",
        EnumFeeder.RECEIPTS,
        "onex.evt.omnimarket.work-state-receipts-fed.v1",
        "work_state.scheduled_feed_receipts",
        id="receipts",
    ),
]


def _contract(node: str) -> dict:
    return yaml.safe_load((SRC / node / "contract.yaml").read_text(encoding="utf-8"))


def _overlay(node: str) -> ModelFeederScheduleOverlay:
    fixture = (
        Path(__file__).resolve().parents[3]
        / f"tests/nodes/{node}/fixtures/overlays/{node}/overlay.yaml"
    )
    return ModelFeederScheduleOverlay.model_validate(
        yaml.safe_load(fixture.read_text(encoding="utf-8"))
    )


def _handler(node: str, module: str, cls: str, port: ProtocolWorkStateFeeder) -> Any:
    """The node's handler over the in-memory overlay and the caller's fake port."""
    handler_cls = getattr(importlib.import_module(module), cls)
    return handler_cls(schedule=_overlay(node), feeder_port=port)


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid.uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


class FakeFeeder:
    """A feeder port with one scripted outcome; records the moments it was asked."""

    def __init__(self, outcome: ModelFeederOutcome | Exception) -> None:
        self._outcome = outcome
        self.calls: list[datetime] = []

    def feed(self, now: datetime) -> ModelFeederOutcome:
        self.calls.append(now)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


# --- (1) the contract's topics, output model and handler ------------------------


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_contract_subscribes_the_tick_and_publishes_only_its_terminal_topic(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    loaded = _contract(node)
    assert loaded["terminal_event"] == terminal
    assert loaded["input_subscriptions"] == [{"topic": TICK, "operation": operation}]
    assert TICK in loaded["event_bus"]["subscribe_topics"]
    assert loaded["event_bus"]["publish_topics"] == [terminal]
    assert loaded["published_events"][0]["topic"] == terminal


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_no_other_node_publishes_this_feeder_terminal_topic(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    publishers = sorted(
        path.parent.name
        for path in SRC.glob("*/contract.yaml")
        if terminal
        in (
            (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("event_bus")
            or {}
        ).get("publish_topics", ())
    )
    assert publishers == [node]


@pytest.mark.parametrize(
    ("node", "module", "cls"),
    [pytest.param(*c.values[:3], id=c.id) for c in CASES],
)
def test_the_output_model_is_the_shared_feeder_event_and_the_handler_is_importable(
    node: str, module: str, cls: str
) -> None:
    contract = _contract(node)
    assert contract["output_model"]["name"] == ModelWorkStateFeederEvent.__name__
    assert (
        contract["output_model"]["module"]
        == "omnimarket.models.work_state_feeder.model_work_state_feeder"
    )
    assert contract["handler"]["class"] == cls
    assert contract["handler"]["module"] == module
    getattr(importlib.import_module(module), cls)


@pytest.mark.parametrize(
    "node",
    [pytest.param(c.values[0], id=c.id) for c in CASES],
)
def test_the_overlay_fixture_is_an_in_memory_schedule(node: str) -> None:
    schedule = _overlay(node)
    assert schedule.profile is EnumFeederProfile.IN_MEMORY
    assert schedule.run_interval_seconds == 60
    assert schedule.max_source_age_s == 3600


# --- (2) the golden chain: a due tick feeds, the interval holds ------------------


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_golden_a_due_tick_feeds_and_the_interval_holds_the_next_one(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    port = FakeFeeder(ModelFeederOutcome(status="fed", source_as_of=T0, item_count=3))
    h = _handler(node, module, cls, port)
    first = tick(T0)
    event = h.handle(first)
    assert event is not None
    assert event.feeder is feeder
    assert event.status == "fed"
    assert event.tick_id == str(first.tick_id)
    assert event.computed_at == T0
    assert event.profile is EnumFeederProfile.IN_MEMORY
    assert event.source_as_of == T0
    assert event.item_count == 3
    assert event.cause == ""
    assert h.handle(tick(T0 + timedelta(seconds=30))) is None
    assert port.calls == [T0]
    second = h.handle(tick(T0 + timedelta(seconds=60)))
    assert second is not None
    assert second.status == "fed"
    assert port.calls == [T0, T0 + timedelta(seconds=60)]


# --- (3) the error chain: failed, raised, stale ---------------------------------


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_error_a_failed_outcome_is_reported_with_its_cause(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    port = FakeFeeder(
        ModelFeederOutcome(status="failed", item_count=0, cause="snapshot refused")
    )
    event = _handler(node, module, cls, port).handle(tick(T0))
    assert event is not None
    assert event.status == "failed"
    assert event.cause == "snapshot refused"
    assert event.source_as_of is None
    assert event.item_count == 0


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_error_a_port_that_raises_propagates_and_the_next_tick_retries(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    port = FakeFeeder(RuntimeError("feeder port refused"))
    h = _handler(node, module, cls, port)
    with pytest.raises(RuntimeError, match="feeder port refused"):
        h.handle(tick(T0))
    with pytest.raises(RuntimeError, match="feeder port refused"):
        h.handle(tick(T0 + timedelta(seconds=1)))
    assert port.calls == [T0, T0 + timedelta(seconds=1)]


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_error_a_source_older_than_the_stale_bound_is_reported_stale(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    aged = T0 - timedelta(seconds=7200)  # the fixture binds max_source_age_s: 3600
    port = FakeFeeder(ModelFeederOutcome(status="fed", source_as_of=aged, item_count=5))
    event = _handler(node, module, cls, port).handle(tick(T0))
    assert event is not None
    assert event.status == "stale"
    assert event.source_as_of == aged
    assert event.item_count == 5


# --- (4) the bus: tick in, event out --------------------------------------------


def serve_tick(handler: Any, raw: bytes) -> dict[str, Any]:
    """The node's consumer: rebuild the tick from the message, run the handler, answer with the event or the error class."""
    try:
        request = ModelRuntimeTick.model_validate_json(raw)
    except ValidationError as exc:
        return {"error": type(exc).__name__, "count": exc.error_count()}
    event = handler.handle(request)
    return {"event": None if event is None else json.loads(event.model_dump_json())}


async def over_inmemory(handler: Any, messages: list[bytes]) -> list[dict[str, Any]]:
    bus = EventBusInmemory(environment="work-state-feeders", group="feeders")
    prefix = uuid.uuid4().hex
    replies: list[dict[str, Any]] = []
    got = asyncio.Event()
    await bus.start()

    async def serve(message: Any) -> None:
        await bus.publish(
            f"{prefix}.evt",
            None,
            json.dumps(serve_tick(handler, message.value)).encode(),
        )

    async def collect(message: Any) -> None:
        replies.append(json.loads(message.value))
        got.set()

    await bus.subscribe(f"{prefix}.tick", on_message=serve, group_id="serve")
    await bus.subscribe(f"{prefix}.evt", on_message=collect, group_id="collect")
    try:
        for raw in messages:
            got.clear()
            await bus.publish(f"{prefix}.tick", None, raw)
            await asyncio.wait_for(got.wait(), TIMEOUT_S)
    finally:
        await bus.close()
    return replies


async def over_redpanda(handler: Any, messages: list[bytes]) -> list[dict[str, Any]]:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    bootstrap = os.environ[BOOTSTRAP_ENV]
    prefix = f"work-state-feeders-{uuid.uuid4().hex}"
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap, security_protocol="PLAINTEXT"
    )
    await producer.start()
    replies: list[dict[str, Any]] = []
    got = asyncio.Event()
    consumers: list[Any] = []

    async def consume(topic: str, handle: Any) -> None:
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap,
            security_protocol="PLAINTEXT",
            group_id=f"{prefix}-{topic}",
            auto_offset_reset="earliest",
        )
        await consumer.start()
        consumers.append(consumer)
        async for message in consumer:
            await handle(message.value)

    async def serve(raw: bytes) -> None:
        await producer.send_and_wait(
            f"{prefix}.evt", json.dumps(serve_tick(handler, raw)).encode()
        )

    async def collect(raw: bytes) -> None:
        replies.append(json.loads(raw))
        got.set()

    tasks = [
        asyncio.create_task(consume(f"{prefix}.tick", serve)),
        asyncio.create_task(consume(f"{prefix}.evt", collect)),
    ]
    try:
        await asyncio.sleep(3.0)  # let the consumers join their topics
        for raw in messages:
            got.clear()
            await producer.send_and_wait(f"{prefix}.tick", raw)
            await asyncio.wait_for(got.wait(), TIMEOUT_S)
    finally:
        for task in tasks:
            task.cancel()
        for consumer in consumers:
            await consumer.stop()
        await producer.stop()
    return replies


def scenario() -> tuple[list[bytes], str]:
    due = tick(T0)
    inside = tick(T0 + timedelta(seconds=30))
    broken = json.dumps({"now": "not a time"}).encode()
    return (
        [
            due.model_dump_json().encode(),
            inside.model_dump_json().encode(),
            broken,
        ],
        str(due.tick_id),
    )


def check(replies: list[dict[str, Any]], feeder: EnumFeeder, tick_id: str) -> None:
    due, inside, broken = replies
    event = ModelWorkStateFeederEvent.model_validate(due["event"])
    assert event.feeder is feeder
    assert event.status == "fed"
    assert event.tick_id == tick_id
    assert event.computed_at == T0
    assert inside == {
        "event": None
    }  # golden: the schedule gate holds a tick inside the interval
    assert (
        broken["error"] == "ValidationError"
    )  # error chain: a tick that does not validate
    assert broken["count"] > 0


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder", "terminal", "operation"), CASES
)
def test_the_in_memory_bus_carries_the_tick_in_and_the_fed_event_out(
    node: str, module: str, cls: str, feeder: EnumFeeder, terminal: str, operation: str
) -> None:
    port = FakeFeeder(ModelFeederOutcome(status="fed", source_as_of=T0, item_count=3))
    messages, tick_id = scenario()
    replies = asyncio.run(over_inmemory(_handler(node, module, cls, port), messages))
    check(replies, feeder, tick_id)


@pytest.mark.parametrize(
    ("node", "module", "cls", "feeder"),
    [pytest.param(*c.values[:4], id=c.id) for c in CASES],
)
@pytest.mark.kafka
@pytest.mark.integration
def test_the_redpanda_bus_carries_the_same_fed_events_as_the_in_memory_bus(
    node: str, module: str, cls: str, feeder: EnumFeeder
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    port = FakeFeeder(ModelFeederOutcome(status="fed", source_as_of=T0, item_count=3))
    messages, tick_id = scenario()
    check(
        asyncio.run(over_redpanda(_handler(node, module, cls, port), messages)),
        feeder,
        tick_id,
    )
