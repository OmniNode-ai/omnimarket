# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader's bus-session route reads replies without joining a consumer group (OMN-26508).

Measured on a lab host (2026-11-24): one lane-liveness read took 7.4 s while the node answered in
0.26 s; ``KafkaTransport.start()`` took 4.07 s joining a fresh uuid group. The route factory now
builds a client whose reply consumer is assigned by hand and positioned at the end.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiokafka.structs import TopicPartition
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    groupless_ledger_query_client as groupless,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.groupless_ledger_query_client import (
    GrouplessLedgerQueryClient,
    GrouplessReplyTransport,
)

pytestmark = pytest.mark.unit

COMPLETED = "onex.evt.omnimarket.work-ledger-query-completed.v1"


def _consumer() -> Mock:
    consumer = Mock()
    consumer.start = AsyncMock()
    consumer.stop = AsyncMock()
    consumer.topics = AsyncMock(return_value={COMPLETED})
    consumer.partitions_for_topic = Mock(return_value={0, 1})
    consumer.seek_to_end = AsyncMock()
    consumer.assign = Mock()
    consumer.getmany = AsyncMock(return_value={})
    consumer._client = SimpleNamespace(set_topics=AsyncMock())
    return consumer


def _transport(
    monkeypatch: pytest.MonkeyPatch, consumer: Mock
) -> tuple[GrouplessReplyTransport, list[object]]:
    group_ids: list[object] = []

    def offset_consumer(config: object, group_id: str | None = None) -> Mock:
        group_ids.append(group_id)
        return consumer

    monkeypatch.setattr(groupless, "create_offset_consumer", offset_consumer)
    transport = GrouplessReplyTransport(
        config=ModelKafkaEventBusConfig(bootstrap_servers="broker.invalid:9092"),
        reply_topic=COMPLETED,
    )
    transport._producer = AsyncMock()
    return transport, group_ids


async def test_reply_consumer_is_assigned_at_the_end_and_joins_no_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = _consumer()
    transport, group_ids = _transport(monkeypatch, consumer)
    await transport.start()
    assert group_ids == [None]
    assigned = set(consumer.assign.call_args.args[0])
    assert assigned == {TopicPartition(COMPLETED, 0), TopicPartition(COMPLETED, 1)}
    consumer.seek_to_end.assert_awaited_once()
    consumer.getmany.assert_not_awaited()
    await transport.close()
    consumer.stop.assert_awaited_once()


async def test_a_reply_topic_the_broker_does_not_list_is_refused_not_read_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = _consumer()
    consumer.partitions_for_topic = Mock(return_value=None)
    transport, _ = _transport(monkeypatch, consumer)
    with pytest.raises(Exception, match="work-ledger-query-completed"):
        await transport.start()
    consumer.stop.assert_awaited_once()


async def test_the_client_answers_a_query_through_the_groupless_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = _consumer()
    sent: dict[str, Any] = {}

    async def send(topic: str, key: object, value: bytes, headers: object) -> None:
        sent.update(json.loads(value))

    async def getmany(**kwargs: object) -> dict[object, list[SimpleNamespace]]:
        payload = dict(sent["payload"])
        record = SimpleNamespace(
            value=json.dumps(
                {
                    "correlation_id": sent["correlation_id"],
                    "payload": {**payload, "freshness": {}, "parity": {}},
                }
            ).encode()
        )
        return {TopicPartition(COMPLETED, 0): [record]}

    consumer.getmany = AsyncMock(side_effect=getmany)
    group_ids: list[object] = []
    monkeypatch.setattr(
        groupless,
        "create_offset_consumer",
        lambda _config, group_id=None: group_ids.append(group_id) or consumer,
    )
    monkeypatch.setattr(groupless.KafkaTransport, "start", AsyncMock())
    monkeypatch.setattr(groupless.KafkaTransport, "close", AsyncMock())
    monkeypatch.setattr(groupless.KafkaTransport, "send", AsyncMock(side_effect=send))
    async with GrouplessLedgerQueryClient(
        ModelKafkaEventBusConfig(bootstrap_servers="broker.invalid:9092")
    ) as client:
        answer = await client.query({"query": "rows"})
    assert answer["query"] == "rows"
    assert group_ids == [None]
