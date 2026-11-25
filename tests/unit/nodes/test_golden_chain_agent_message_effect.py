# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: one agent message goes through node_agent_message_effect to the topic and back to its reader.

The chain runs the node with the production emit effect (the durable spool, the shared publish adapter) and a fake
broker behind the adapter's bus seam: a valid message is spooled and published on the declared topic, a reader of the
same bus gets it back typed. The error leg: an incomplete message is refused and nothing is spooled, and a node whose
overlay declares no broker answers a typed broker_not_declared result and keeps the message on the spool.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import pytest
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation
from omnimarket.enums.enum_agent_message_refusal import EnumAgentMessageRefusal
from omnimarket.models.agent_message.model_agent_message_request import (
    ModelAgentMessageRequest,
)
from omnimarket.nodes.node_agent_message_effect.container.container_agent_message_effect import (
    create_emitter,
)
from omnimarket.nodes.node_agent_message_effect.node import NodeAgentMessageEffect
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.omnimarket.agent-message.v1"
MESSAGE = {
    "from": "lane-a",
    "to": "lane-b",
    "thread": "thread-1",
    "kind": "question",
    "text": "which branch?",
    "sent_at": "2026-11-23T13:30:00+00:00",
    "platform": "codex",
}


class _Broker:
    def __init__(self) -> None:
        self.records: list[bytes] = []

    def publish(self, topic: str, payload: object, **_: object) -> None:
        assert topic == TOPIC
        self.records.append(json.dumps(payload).encode())

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]:
        for value in self.records:
            await on_message(
                ModelEventMessage(
                    topic=topic,
                    key=None,
                    value=value,
                    headers=ModelEventHeaders(
                        source="test",
                        event_type="agent.message",
                        timestamp=datetime.fromisoformat("2026-11-23T13:30:01+00:00"),
                    ),
                )
            )

        async def stop() -> None:
            return None

        return stop


def test_a_sent_message_is_read_back_typed(tmp_path: Path) -> None:
    broker = _Broker()
    node = NodeAgentMessageEffect(
        emitter=HandlerEventEmitEffect(
            publish_adapter=broker, spool_dir=tmp_path / "spool"
        ),
        bus=broker,
        read_window_seconds=0.05,
        quiet_gap_seconds=0.02,
    )
    sent = asyncio.run(
        node.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.SEND, message=dict(MESSAGE)
            )
        )
    )
    assert (sent.accepted, sent.published, sent.topic) == (True, True, TOPIC)
    read = asyncio.run(
        node.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.READ,
                to="lane-b",
                thread="thread-1",
            )
        )
    )
    assert [(m.sender, m.platform.value, m.kind.value) for m in read.messages] == [
        ("lane-a", "codex", "question")
    ]


def test_an_incomplete_message_is_refused_and_nothing_is_spooled(
    tmp_path: Path,
) -> None:
    broker = _Broker()
    spool = tmp_path / "spool"
    node = NodeAgentMessageEffect(
        emitter=HandlerEventEmitEffect(publish_adapter=broker, spool_dir=spool),
        bus=broker,
    )
    incomplete = {k: v for k, v in MESSAGE.items() if k != "platform"}
    result = asyncio.run(
        node.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.SEND, message=incomplete
            )
        )
    )
    assert result.refusal is EnumAgentMessageRefusal.INVALID_MESSAGE
    assert broker.records == []
    assert not spool.exists() or list(spool.glob("*.json")) == []


def test_a_node_with_no_declared_broker_answers_broker_not_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production binding, with an empty workspace: no overlay declares the node's broker."""
    monkeypatch.delenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", raising=False)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path / "empty-workspace"))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "empty-workspace"))
    node = NodeAgentMessageEffect(emitter=create_emitter())
    result = asyncio.run(
        node.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.SEND, message=dict(MESSAGE)
            )
        )
    )
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.BROKER_NOT_DECLARED
    read = asyncio.run(
        node.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.READ, to="lane-b"
            )
        )
    )
    assert read.refusal is EnumAgentMessageRefusal.BROKER_NOT_DECLARED
