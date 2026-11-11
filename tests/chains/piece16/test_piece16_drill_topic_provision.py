# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drill-topic provision the redpanda leg performs, proven without a broker.

``RedpandaChainBus.roundtrip`` creates its disposable drill topic through the admin client before
it starts its producer and consumer, and deletes the topic it created on the way out; a consumer
can only start on a topic that exists in the cluster metadata, so the create precedes both starts.
Fake clients record every call in order against one shared state and never touch a network. The
lane plumbing is monkeypatched to inert values, so the fakes are the only things that move.
"""

from __future__ import annotations

import asyncio
import types

import aiokafka
import pytest
from aiokafka.admin import NewTopic
from omnibase_infra.cli import delegate_lane_credentials as lane_credentials
from omnibase_infra.event_bus import kafka_auth

from tests import bus_lane_guard
from tests.chains.piece16.test_piece16_chain import (
    EMISSION_TOPIC,
    PILOT_COMMAND_TOPIC,
    PILOT_TERMINAL_TOPIC,
    RedpandaChainBus,
)

pytestmark = pytest.mark.unit

# The one address in this module: the broker the fakes are pointed at, never reached.
BROKER = "broker.invalid:1"
TOPIC = "omnimarket.pilot.command"
PAYLOAD = b"piece-16 drill payload"
TOPIC_AUTHORIZATION_FAILED = 29


class ClusterState:
    """The shared memory of the fakes: the events in call order, the one message in flight, the created topics."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.sent: bytes | None = None
        self.created: set[str] = set()
        self.refuse_create = False


STATE = ClusterState()


class FakeCreateResponse:
    def __init__(self, topic_errors: list[tuple[str, int, str]]) -> None:
        self.topic_errors = topic_errors


class FakeAdmin:
    def __init__(self, bootstrap_servers: str = "", **auth: object) -> None: ...

    async def start(self) -> None:
        STATE.events.append("admin.start")

    async def create_topics(self, topics: list[NewTopic]) -> FakeCreateResponse:
        errors: list[tuple[str, int, str]] = []
        for topic in topics:
            STATE.events.append(f"admin.create:{topic.name}")
            if STATE.refuse_create:
                code, err = TOPIC_AUTHORIZATION_FAILED, "TOPIC_AUTHORIZATION_FAILED"
                errors.append((topic.name, code, err))
            else:
                STATE.created.add(topic.name)
        return FakeCreateResponse(errors)

    async def delete_topics(self, topics: list[str]) -> None:
        for topic in topics:
            STATE.events.append(f"admin.delete:{topic}")

    async def close(self) -> None:
        STATE.events.append("admin.close")


class FakeProducer:
    def __init__(self, bootstrap_servers: str = "", **auth: object) -> None: ...

    async def start(self) -> None:
        STATE.events.append("producer.start")

    async def send_and_wait(self, topic: str, value: bytes) -> None:
        STATE.events.append(f"producer.send:{topic}")
        STATE.sent = value

    async def stop(self) -> None:
        STATE.events.append("producer.stop")


class FakeConsumer:
    """A fake broker model: a consumer can only start on a topic the admin has created earlier."""

    def __init__(
        self, *topics: str, bootstrap_servers: str = "", **auth: object
    ) -> None:
        self._topics = topics

    async def start(self) -> None:
        if any(topic in STATE.created for topic in self._topics):
            STATE.events.append("consumer.start")
            return
        raise TimeoutError(f"topic not found in cluster metadata: {self._topics[0]!r}")

    async def getone(self) -> types.SimpleNamespace:
        STATE.events.append("consumer.getone")
        if STATE.sent is None:
            raise AssertionError("getone before any send")
        return types.SimpleNamespace(value=STATE.sent)

    async def stop(self) -> None:
        STATE.events.append("consumer.stop")


def install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    for (module, name), value in (
        ((aiokafka, "AIOKafkaProducer"), FakeProducer),
        ((aiokafka, "AIOKafkaConsumer"), FakeConsumer),
        ((aiokafka.admin, "AIOKafkaAdminClient"), FakeAdmin),
        ((lane_credentials, "resolve_lane_binding"), lambda _lane: (None, None)),
        ((kafka_auth, "build_aiokafka_auth_kwargs_for"), lambda _bootstrap: {}),
        ((bus_lane_guard, "resolve_test_bus_broker"), lambda: BROKER),
        ((bus_lane_guard, "assert_broker_in_declared_lane"), lambda broker: broker),
    ):
        monkeypatch.setattr(module, name, value)
    STATE.events.clear()
    STATE.sent, STATE.created, STATE.refuse_create = None, set(), False


def test_golden_the_topic_is_created_before_the_consumer_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fakes(monkeypatch)
    got = asyncio.run(RedpandaChainBus().roundtrip(TOPIC, PAYLOAD))
    assert got == PAYLOAD
    events = STATE.events
    drill_topic = events[1].split("admin.create:", 1)[1]
    assert events == [
        "admin.start",
        f"admin.create:{drill_topic}",
        "producer.start",
        "consumer.start",
        f"producer.send:{drill_topic}",
        "consumer.getone",
        "consumer.stop",
        "producer.stop",
        f"admin.delete:{drill_topic}",
        "admin.close",
    ]


def test_error_a_refused_create_fails_the_leg_and_starts_no_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fakes(monkeypatch)
    STATE.refuse_create = True
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(RedpandaChainBus().roundtrip(TOPIC, PAYLOAD))
    assert "29" in str(exc.value)
    events = STATE.events
    assert "producer.start" not in events
    assert "consumer.start" not in events
    assert not any(event.startswith("admin.delete") for event in events)
    assert len(events) == 3
    assert events[0] == "admin.start"
    assert events[2] == "admin.close"
    assert events[1].startswith("admin.create:")


async def old_order_roundtrip(topic: str, payload: bytes) -> bytes:
    admin = FakeAdmin(bootstrap_servers=BROKER)
    producer = FakeProducer(bootstrap_servers=BROKER)
    consumer = FakeConsumer(topic, bootstrap_servers=BROKER)
    await admin.start()
    await producer.start()
    await consumer.start()
    await producer.send_and_wait(topic, value=payload)
    message = await consumer.getone()
    await consumer.stop()
    await producer.stop()
    await admin.close()
    return bytes(message.value)


def test_control_without_the_create_the_consumer_waits_on_a_missing_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fakes(monkeypatch)
    with pytest.raises(TimeoutError):
        asyncio.run(old_order_roundtrip(TOPIC, PAYLOAD))
    assert "consumer.start" not in STATE.events
    STATE.events.clear()
    got = asyncio.run(RedpandaChainBus().roundtrip(TOPIC, PAYLOAD))
    assert got == PAYLOAD
    create = next(event for event in STATE.events if event.startswith("admin.create:"))
    assert STATE.events.index(create) < STATE.events.index("consumer.start")


# The seam with the overlays repository: the five topics the leg sends on, spelled here and in that repository's own
# test (tests/test_broker_acl_overlays.py, PIECE16_DRILL_PREFIXES), whose rows give the lab logins CREATE and DELETE on
# each name plus the per-run drill suffix. A change to a contract topic fails this file; a change to the declared rows
# fails the overlays test.
DECLARED_DRILL_TOPICS = {
    "onex.cmd.omnimarket.pilot-manifest-check-requested.v1",
    "onex.evt.omnimarket.pilot-manifest-checked.v1",
    "onex.evt.omnimarket.floor-node-started.v1",
    "onex.evt.omnimarket.floor-node-succeeded.v1",
    "onex.evt.omnimarket.floor-node-failed.v1",
}


def test_seam_the_topics_the_leg_sends_on_are_the_ones_the_overlay_rows_declare() -> (
    None
):
    used = {PILOT_COMMAND_TOPIC, PILOT_TERMINAL_TOPIC, *EMISSION_TOPIC.values()}
    assert used == DECLARED_DRILL_TOPICS
