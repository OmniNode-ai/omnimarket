# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's topic reader opens the declared lane or lab broker and reads the topic (OMN-23458)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_topic_reader import (
    BusTickTopicReader,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickTopicSelection,
)
from omnimarket.topic_namespace import apply_topic_namespace

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"
GROUP = "landing-tick-observe"


class _Message:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _Handle:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakeBus:
    def __init__(self, messages: list[_Message]) -> None:
        self._messages = messages
        self.handle = _Handle()
        self.topic: str | None = None
        self.group_id: str | None = None
        self.auto_offset_reset: str | None = None

    async def __aenter__(self) -> _FakeBus:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[_Message], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
        auto_offset_reset: str | None = None,
    ) -> _Handle:
        self.topic = topic
        self.group_id = group_id
        self.auto_offset_reset = auto_offset_reset
        if on_message is not None:
            for message in self._messages:
                await on_message(message)
        return self.handle


class _FakeOpener:
    def __init__(self, messages: list[_Message]) -> None:
        self.messages = messages
        self.kwargs: dict[str, object] = {}
        self.bus = _FakeBus(self.messages)

    def __call__(self, **kwargs: object) -> _FakeOpener:
        self.kwargs = kwargs
        return self

    async def __aenter__(self) -> _FakeBus:
        return self.bus

    async def __aexit__(self, *exc: object) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch, messages: list[_Message]) -> _FakeOpener:
    opener = _FakeOpener(messages)
    monkeypatch.setattr(
        "omnimarket.handlers.overlay_store.bus_opener.open_lane_bus", opener
    )
    return opener


def test_the_reader_opens_the_declared_lane_and_reads_from_the_earliest_record_under_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [_Message(f"{i}".encode()) for i in range(3)]
    opener = _install(monkeypatch, messages)
    selection = ModelTickTopicSelection(
        join_timeout_s=1.0,
        topic=TOPIC,
        consumer_group=GROUP,
        bus_lane="dev",
        workspace_root="/ws",
    )
    got = BusTickTopicReader(selection).read(10, 0.2)
    assert got == ["0", "1", "2"]
    assert opener.kwargs == {
        "bus": "kafka",
        "lane": "dev",
        "kafka_bootstrap": None,
        "root": Path("/ws"),
    }
    bus = opener.bus
    assert bus.topic == apply_topic_namespace(TOPIC)
    assert bus.group_id == GROUP
    assert bus.auto_offset_reset == "earliest"
    assert bus.handle.stopped is True


def test_the_reader_stops_at_max_records(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [_Message(f"{i}".encode()) for i in range(5)]
    _install(monkeypatch, messages)
    selection = ModelTickTopicSelection(
        join_timeout_s=1.0,
        topic=TOPIC,
        consumer_group=GROUP,
        bus_lane="dev",
        workspace_root="/ws",
    )
    assert BusTickTopicReader(selection).read(2, 0.2) == ["0", "1"]


def test_a_plaintext_broker_selection_opens_no_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _install(monkeypatch, [])
    selection = ModelTickTopicSelection(
        join_timeout_s=1.0,
        topic=TOPIC,
        consumer_group=GROUP,
        bootstrap_servers="broker.example:9092",
    )
    BusTickTopicReader(selection).read(5, 0.05)
    assert opener.kwargs["lane"] is None
    assert opener.kwargs["kafka_bootstrap"] == "broker.example:9092"


def test_an_empty_topic_returns_nothing_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _install(monkeypatch, [])
    selection = ModelTickTopicSelection(
        join_timeout_s=1.0,
        topic=TOPIC,
        consumer_group=GROUP,
        bus_lane="dev",
        workspace_root="/ws",
    )
    assert BusTickTopicReader(selection).read(5, 0.05) == []
    assert opener.bus.handle.stopped is True
