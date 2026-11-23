# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lease chain's broker calls, held to the grants the dev lane principal needs (seam L6, OMN-24484).

The three lease topics are new, so the dev-lane principal's bindings in the lab infrastructure
declaration do not name them yet: the real-broker run of the golden and error chains reads
``TopicAuthorizationFailedError`` until they do. This test spells the grants the chain needs (the
command topic and the recorded terminal, and two consumer-group prefixes) and holds the chain's
broker-facing bus against them with a recording client, so the request to the owner of the declaration
is exactly what the chain uses, and a change to a topic or a group prefix reads red here before the
broker is first to refuse it. The refused terminal is never used by the broker-backed bus.
"""

from __future__ import annotations

import contextlib
import functools
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

import pytest

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from tests.chain.lease_chain import (
    ModelLeaseChainOverlay,
    RedpandaLeaseBus,
    lease_topics,
)

pytestmark = pytest.mark.unit

NEEDED_COMMAND_TOPIC: Final = "onex.cmd.omnimarket.lease-receipt-requested.v1"
NEEDED_RECORDED_TOPIC: Final = "onex.evt.omnimarket.lease-receipt-recorded.v1"
NEEDED_REFUSED_TOPIC: Final = "onex.evt.omnimarket.lease-receipt-refused.v1"
NEEDED_TOPICS: Final[frozenset[str]] = frozenset(
    {NEEDED_COMMAND_TOPIC, NEEDED_RECORDED_TOPIC}
)
NEEDED_GROUP_PREFIXES: Final[tuple[str, str]] = (
    "lease-chain-node-",
    "lease-chain-controller-",
)


def assert_grant_shape(
    subscriptions: Sequence[Mapping[str, Any]],
    publishes: Sequence[Mapping[str, Any]],
    *,
    run_id: str | None = None,
) -> None:
    for sub in subscriptions:
        assert sub["topic"] in NEEDED_TOPICS, f"subscribed {sub['topic']!r}"
        assert sub["group_id"].startswith(NEEDED_GROUP_PREFIXES), sub["group_id"]
        if run_id is not None:
            assert sub["group_id"].endswith(run_id), sub["group_id"]
    for published in publishes:
        assert published["topic"] in NEEDED_TOPICS, f"published {published['topic']!r}"
    carried = {s["topic"] for s in subscriptions} | {p["topic"] for p in publishes}
    assert NEEDED_REFUSED_TOPIC not in carried, "the refused terminal left the bus"


class FakeLabRunBus:
    def __init__(self) -> None:
        self.subscriptions: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []

    async def subscribe(self, topic: str, *, on_message: Any, group_id: str) -> None:
        self.subscriptions.append(
            {"topic": topic, "on_message": on_message, "group_id": group_id}
        )

    async def start_consuming(self) -> None:
        return None

    async def publish(self, *, topic: str, key: bytes, value: bytes) -> None:
        self.published.append({"topic": topic, "key": key, "value": value})


async def _started_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeLabRunBus, RedpandaLeaseBus]:
    fake = FakeLabRunBus()

    @contextlib.asynccontextmanager
    @functools.wraps(open_lab_run_bus)
    async def fake_open_lab_run_bus(**_: Any) -> AsyncIterator[FakeLabRunBus]:
        yield fake

    monkeypatch.setattr(
        "omnimarket.delegated_test_loop.lane_bus.open_lab_run_bus",
        fake_open_lab_run_bus,
    )
    bus = RedpandaLeaseBus(
        ModelLeaseChainOverlay(profile="redpanda", lane="dev", consumer_join_s=0)
    )
    await bus.start()
    return fake, bus


async def test_lease_bus_subscribes_only_under_needed_topics_and_group_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bus = await _started_bus(monkeypatch)
    try:
        assert_grant_shape(fake.subscriptions, fake.published, run_id=bus.run_id)
        assert [(s["topic"], s["group_id"]) for s in fake.subscriptions] == [
            (bus.topic_command, f"{NEEDED_GROUP_PREFIXES[0]}{bus.run_id}"),
            (bus.topic_recorded, f"{NEEDED_GROUP_PREFIXES[1]}{bus.run_id}"),
        ]
    finally:
        await bus.stop()


async def test_lease_bus_publishes_only_needed_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bus = await _started_bus(monkeypatch)
    try:
        await bus._send(bus.topic_command, b"command")
        await bus._send(bus.topic_recorded, b"reply")
        assert_grant_shape(fake.subscriptions, fake.published, run_id=bus.run_id)
    finally:
        await bus.stop()


def test_needed_topics_equal_the_node_contract() -> None:
    topics = lease_topics()
    assert topics["command"] == NEEDED_COMMAND_TOPIC
    assert topics["recorded"] == NEEDED_RECORDED_TOPIC
    assert topics["refused"] == NEEDED_REFUSED_TOPIC


def test_control_a_changed_group_prefix_is_red() -> None:
    off_grant: list[dict[str, Any]] = [
        {
            "topic": NEEDED_COMMAND_TOPIC,
            "on_message": None,
            "group_id": f"lease-chain-other-{uuid.uuid4().hex}",
        }
    ]
    with pytest.raises(AssertionError):
        assert_grant_shape(off_grant, [])


def test_control_the_refused_terminal_on_the_broker_bus_is_red() -> None:
    with pytest.raises(AssertionError):
        assert_grant_shape([], [{"topic": NEEDED_REFUSED_TOPIC}])
