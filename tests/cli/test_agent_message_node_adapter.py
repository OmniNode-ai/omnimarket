# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The client's bus path drives the agent message node (OMN-26582): the node factory is no longer a refusal.

The node's handler runs over a fake bus; the client sends through ``agent_message_node_factory`` and reads what
it sent back, so the seam between the two nodes (kinds, platforms, wire spelling, message id) is the thing under test.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from omnimarket.handlers.bus_binding.node_broker import NodeBrokerResolutionError
from omnimarket.nodes.node_agent_message_client_effect.adapter_bus_node import (
    agent_message_node_factory,
)
from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    EnumAgentMessageKind,
    EnumAgentMessagePlatform,
    ModelAgentMessage,
    ModelAgentMessageQuery,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    AgentMessageRefusalError,
)
from omnimarket.nodes.node_agent_message_client_effect.models.refusal import (
    EnumAgentMessageRefusal as EnumClientRefusal,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from tests.cli.agent_message_fakes import FIXED_NOW
from tests.unit.nodes.node_agent_message_effect.conftest import FakeBus

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _spool_only_lane(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _message(**updates: object) -> ModelAgentMessage:
    fields: dict[str, object] = {
        "message_id": "client-side-id",
        "sender": "lane-a",
        "to": "lane-b",
        "thread": "thread-1",
        "kind": EnumAgentMessageKind.QUESTION,
        "platform": EnumAgentMessagePlatform.CODEX,
        "text": "which branch?",
        "sent_at": FIXED_NOW,
    }
    fields.update(updates)
    return ModelAgentMessage.model_validate(fields)


def _node(
    handler: HandlerAgentMessage,
) -> contextlib.AbstractAsyncContextManager[object]:
    factory = agent_message_node_factory(handler_factory=lambda: handler)
    return factory(object())  # type: ignore[arg-type]


def _run_send(handler: HandlerAgentMessage, message: ModelAgentMessage) -> str | None:
    async def go() -> str | None:
        async with _node(handler) as node:
            return await node.send(message)  # type: ignore[attr-defined, no-any-return]

    return asyncio.run(go())


def _run_read(
    handler: HandlerAgentMessage, query: ModelAgentMessageQuery
) -> tuple[ModelAgentMessage, ...]:
    async def go() -> tuple[ModelAgentMessage, ...]:
        async with _node(handler) as node:
            return await node.read(query)  # type: ignore[attr-defined, no-any-return]

    return asyncio.run(go())


def test_the_factory_no_longer_refuses_node_unavailable() -> None:
    agent_message_node_factory()


def test_send_publishes_the_wire_message_and_returns_the_event_id(
    handler: HandlerAgentMessage, bus: FakeBus
) -> None:
    event_id = _run_send(handler, _message())
    assert event_id
    assert bus.published_topics == ["onex.evt.omnimarket.agent-message.v1"]


def test_send_then_read_round_trips_with_the_event_id_as_the_message_id(
    handler: HandlerAgentMessage,
) -> None:
    event_id = _run_send(handler, _message())
    read = _run_read(handler, ModelAgentMessageQuery(to="lane-b", thread="thread-1"))
    assert [m.text for m in read] == ["which branch?"]
    assert read[0].message_id == event_id
    assert read[0].sender == "lane-a"
    assert read[0].kind is EnumAgentMessageKind.QUESTION
    assert read[0].platform is EnumAgentMessagePlatform.CODEX
    assert read[0].sent_at == FIXED_NOW


def test_a_read_limit_above_the_node_maximum_is_clamped_not_refused(
    handler: HandlerAgentMessage,
) -> None:
    _run_send(handler, _message())
    read = _run_read(handler, ModelAgentMessageQuery(to="lane-b", limit=1000))
    assert len(read) == 1


def test_a_message_only_spooled_is_a_broker_refusal(tmp_path: Path) -> None:
    class DownBroker:
        def publish(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("broker down")

    emitter = HandlerEventEmitEffect(
        publish_adapter=DownBroker(), spool_dir=tmp_path / "spool"
    )
    down = HandlerAgentMessage(emitter=emitter, bus=FakeBus())
    with pytest.raises(AgentMessageRefusalError) as caught:
        _run_send(down, _message())
    assert caught.value.code is EnumClientRefusal.BROKER_REFUSED


def test_a_node_with_no_declared_broker_is_undeclared_broker(
    tmp_path: Path,
) -> None:
    class Undeclared:
        def handle(self, request: object) -> object:
            raise NodeBrokerResolutionError(
                "node_agent_message_effect declares no event_bus"
            )

    undeclared = HandlerAgentMessage(emitter=Undeclared(), bus=FakeBus())  # type: ignore[arg-type]
    with pytest.raises(AgentMessageRefusalError) as caught:
        _run_send(undeclared, _message())
    assert caught.value.code is EnumClientRefusal.UNDECLARED_BROKER


def test_the_client_kinds_and_platforms_are_the_nodes() -> None:
    from omnimarket.enums.enum_agent_message_kind import (
        EnumAgentMessageKind as NodeKind,
    )
    from omnimarket.enums.enum_agent_message_platform import (
        EnumAgentMessagePlatform as NodePlatform,
    )

    assert EnumAgentMessageKind is NodeKind
    assert EnumAgentMessagePlatform is NodePlatform
