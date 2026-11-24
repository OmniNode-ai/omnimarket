# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures for the agent message node tests: a fake bus that stores what is published and replays it to a reader."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)


class FakeBus:
    """Both sides of the bus in memory: the emit effect's publish adapter and the reader's subscribe."""

    def __init__(self) -> None:
        self.records: list[tuple[str, bytes]] = []
        self.published_topics: list[str] = []
        self.subscriptions: list[tuple[str, str, str]] = []

    def publish(
        self,
        topic: str,
        payload: object,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.published_topics.append(topic)
        self.records.append((topic, json.dumps(payload).encode()))

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]:
        self.subscriptions.append((topic, group_id, auto_offset_reset))
        for stored_topic, value in self.records:
            if stored_topic == topic:
                await on_message(
                    ModelEventMessage(
                        topic=topic,
                        key=None,
                        value=value,
                        headers=ModelEventHeaders(
                            source="test",
                            event_type="agent.message",
                            timestamp=datetime.now(UTC),
                        ),
                    )
                )

        async def stop() -> None:
            return None

        return stop


@pytest.fixture(autouse=True)
def _spool_only_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """No broker in unit tests: the declared spool-only opt-out; the fake bus is injected explicitly."""
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def handler(bus: FakeBus, tmp_path: Path) -> HandlerAgentMessage:
    emitter = HandlerEventEmitEffect(publish_adapter=bus, spool_dir=tmp_path / "spool")
    return HandlerAgentMessage(
        emitter=emitter, bus=bus, read_window_seconds=0.05, quiet_gap_seconds=0.02
    )


def message_fields(**updates: object) -> dict[str, object]:
    """One valid message, in the wire spelling (the sender cell is ``from``)."""
    fields: dict[str, object] = {
        "from": "lane-a",
        "to": "lane-b",
        "thread": "thread-1",
        "kind": "message",
        "text": "please look at the branch",
        "sent_at": "2026-11-23T13:30:00+00:00",
        "platform": "claude",
    }
    fields.update(updates)
    return fields
