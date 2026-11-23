# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's two operations against a fake bus: refusals publish nothing; send then read round-trips."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation
from omnimarket.enums.enum_agent_message_refusal import EnumAgentMessageRefusal
from omnimarket.handlers.bus_binding.node_broker import NodeBrokerResolutionError
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
)
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_request import (
    ModelAgentMessageRequest,
)
from tests.unit.nodes.node_agent_message_effect.conftest import FakeBus, message_fields

pytestmark = pytest.mark.unit

SEND = EnumAgentMessageOperation.SEND
READ = EnumAgentMessageOperation.READ


def _send(handler: HandlerAgentMessage, **updates: object):  # type: ignore[no-untyped-def]
    return asyncio.run(
        handler.handle(
            ModelAgentMessageRequest(operation=SEND, message=message_fields(**updates))
        )
    )


def _read(handler: HandlerAgentMessage, **fields: object):  # type: ignore[no-untyped-def]
    return asyncio.run(
        handler.handle(ModelAgentMessageRequest(operation=READ, **fields))  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "missing", ["from", "to", "thread", "kind", "text", "sent_at", "platform"]
)
def test_an_incomplete_message_is_refused_and_nothing_is_published(
    handler: HandlerAgentMessage, bus: FakeBus, missing: str
) -> None:
    fields = message_fields()
    del fields[missing]
    result = asyncio.run(
        handler.handle(ModelAgentMessageRequest(operation=SEND, message=fields))
    )
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.INVALID_MESSAGE
    assert missing in (result.detail or "")
    assert bus.records == []


def test_an_unknown_platform_is_refused_and_nothing_is_published(
    handler: HandlerAgentMessage, bus: FakeBus
) -> None:
    result = _send(handler, platform="mistral")
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.INVALID_MESSAGE
    assert bus.records == []


def test_a_naive_sent_at_is_refused_and_nothing_is_published(
    handler: HandlerAgentMessage, bus: FakeBus
) -> None:
    result = _send(handler, sent_at="2026-11-23T13:30:00")
    assert result.refusal is EnumAgentMessageRefusal.INVALID_MESSAGE
    assert bus.records == []


def test_a_valid_message_is_published_once_to_the_declared_topic(
    handler: HandlerAgentMessage, bus: FakeBus
) -> None:
    result = _send(handler)
    assert result.accepted is True
    assert result.published is True
    assert result.refusal is None
    assert bus.published_topics == [result.topic]
    assert result.topic == "onex.evt.omnimarket.agent-message.v1"


def test_send_then_read_round_trips_by_recipient_and_thread(
    handler: HandlerAgentMessage,
) -> None:
    _send(handler, text="one")
    _send(handler, text="elsewhere", thread="thread-2")
    _send(handler, text="not for b", to="lane-c")
    _send(handler, text="two", sent_at="2026-11-23T13:31:00+00:00")
    result = _read(handler, to="lane-b", thread="thread-1")
    assert result.accepted is True
    assert [m.text for m in result.messages] == ["one", "two"]
    assert all(m.to == "lane-b" and m.thread == "thread-1" for m in result.messages)


def test_read_without_a_thread_returns_every_thread_for_the_recipient(
    handler: HandlerAgentMessage,
) -> None:
    _send(handler, text="one")
    _send(handler, text="elsewhere", thread="thread-2")
    result = _read(handler, to="lane-b")
    assert sorted(m.text for m in result.messages) == ["elsewhere", "one"]


def test_read_returns_oldest_first_whatever_the_publish_order(
    handler: HandlerAgentMessage,
) -> None:
    _send(handler, text="late", sent_at="2026-11-23T14:00:00+00:00")
    _send(handler, text="early", sent_at="2026-11-23T13:00:00+00:00")
    result = _read(handler, to="lane-b")
    assert [m.text for m in result.messages] == ["early", "late"]


def test_read_of_an_empty_thread_returns_empty(handler: HandlerAgentMessage) -> None:
    _send(handler)
    result = _read(handler, to="lane-b", thread="no-such-thread")
    assert result.accepted is True
    assert result.messages == ()
    assert result.refusal is None


def test_read_of_an_empty_bus_returns_empty(handler: HandlerAgentMessage) -> None:
    result = _read(handler, to="lane-b")
    assert result.accepted is True
    assert result.messages == ()


def test_a_resent_message_is_read_once(handler: HandlerAgentMessage) -> None:
    _send(handler)
    _send(handler)
    assert len(_read(handler, to="lane-b").messages) == 1


def test_read_is_bounded_by_since(handler: HandlerAgentMessage) -> None:
    _send(handler, text="old", sent_at="2026-11-23T12:00:00+00:00")
    _send(handler, text="new", sent_at="2026-11-23T13:30:00+00:00")
    result = _read(
        handler, to="lane-b", since=datetime.fromisoformat("2026-11-23T13:00:00+00:00")
    )
    assert [m.text for m in result.messages] == ["new"]


def test_read_is_bounded_by_count_and_keeps_the_newest(
    handler: HandlerAgentMessage,
) -> None:
    for minute in range(5):
        _send(handler, text=f"m{minute}", sent_at=f"2026-11-23T13:0{minute}:00+00:00")
    result = _read(handler, to="lane-b", limit=2)
    assert [m.text for m in result.messages] == ["m3", "m4"]


def test_a_naive_since_is_refused_by_the_request() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ModelAgentMessageRequest(
            operation=READ, to="lane-b", since=datetime(2026, 11, 23, 13, 0)
        )


def test_read_without_a_recipient_is_refused_by_the_request() -> None:
    with pytest.raises(ValueError, match="needs a recipient"):
        ModelAgentMessageRequest(operation=READ)


def test_send_without_a_message_is_refused_by_the_request() -> None:
    with pytest.raises(ValueError, match="needs a message"):
        ModelAgentMessageRequest(operation=SEND)


def test_a_record_that_is_not_a_message_is_skipped_on_read(
    handler: HandlerAgentMessage, bus: FakeBus
) -> None:
    _send(handler)
    bus.records.append((bus.records[0][0], b"not json"))
    bus.records.append((bus.records[0][0], b'{"payload": {"to": "lane-b"}}'))
    result = _read(handler, to="lane-b")
    assert len(result.messages) == 1
    assert result.skipped == 2


def test_a_node_with_no_declared_broker_answers_broker_not_declared(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    class NoBroker:
        def handle(self, request):  # type: ignore[no-untyped-def]
            raise NodeBrokerResolutionError("no event_bus declared for node")

    handler = HandlerAgentMessage(emitter=NoBroker(), bus=FakeBus())
    result = _send(handler)
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.BROKER_NOT_DECLARED


def test_a_read_with_no_declared_broker_answers_broker_not_declared() -> None:
    def no_bus() -> FakeBus:
        raise NodeBrokerResolutionError("no event_bus declared for node")

    handler = HandlerAgentMessage(bus_factory=no_bus)
    result = _read(handler, to="lane-b")
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.BROKER_NOT_DECLARED


def test_a_failed_publish_is_a_typed_refusal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Boom:
        def handle(self, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("spool refused")

    result = _send(HandlerAgentMessage(emitter=Boom(), bus=FakeBus()))
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.PUBLISH_FAILED


def test_the_reader_uses_a_fresh_group_per_read_from_earliest(
    handler: HandlerAgentMessage, bus: FakeBus
) -> None:
    _read(handler, to="lane-b")
    _read(handler, to="lane-b")
    (topic, group_a, reset_a), (_, group_b, reset_b) = bus.subscriptions
    assert topic == "onex.evt.omnimarket.agent-message.v1"
    assert group_a != group_b
    assert group_a.startswith("agent-message-read-")
    assert reset_a == reset_b == "earliest"
