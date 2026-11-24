# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reader read through the bus-session route builds no consumer-group consumer (OMN-26508).

The route is driven the way the reader runs it: ``build_point_read`` over a bus-session route,
the real infra query client, and a fake aiokafka under it. The fake records every consumer built;
a consumer built with a group id is a group join (4.07 s of a 7.4 s read on a lab host).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiokafka.structs import TopicPartition
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import build_point_read
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelBusSessionReadRoute,
    ModelPointQuery,
)

pytestmark = pytest.mark.unit

COMPLETED = "onex.evt.omnimarket.work-ledger-query-completed.v1"


def test_a_window_read_joins_no_consumer_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, Any] = {}
    group_ids: list[object] = []

    async def send(topic: str, **kwargs: Any) -> SimpleNamespace:
        value = kwargs["value"]
        sent.update(json.loads(value))
        return SimpleNamespace(topic=COMPLETED, partition=0, offset=0)

    async def getmany(**kwargs: object) -> dict[TopicPartition, list[SimpleNamespace]]:
        if not sent:
            return {}
        record = SimpleNamespace(
            value=json.dumps(
                {
                    "correlation_id": sent["correlation_id"],
                    "payload": {
                        **sent["payload"],
                        "matched": 0,
                        "lines": [],
                        "freshness": {"newest_projected_at": "2026-11-24T00:00:00Z"},
                        "parity": {},
                    },
                }
            ).encode(),
            topic=COMPLETED,
            partition=0,
            offset=0,
            key=None,
            headers=[],
        )
        return {TopicPartition(COMPLETED, 0): [record]}

    consumer = AsyncMock()
    consumer.assignment = lambda: {TopicPartition(COMPLETED, 0)}
    consumer.getmany.side_effect = getmany
    consumer.partitions_for_topic = Mock(return_value={0})
    consumer.assign = Mock()
    consumer._client = SimpleNamespace(set_topics=AsyncMock())
    producer = AsyncMock()
    producer.send_and_wait.side_effect = send

    def make_consumer(*args: object, **kwargs: Any) -> AsyncMock:
        group_ids.append(kwargs.get("group_id"))
        return consumer

    monkeypatch.setattr(
        "omnibase_infra.event_bus.kafka_transport.AIOKafkaConsumer", make_consumer
    )
    monkeypatch.setattr(
        "omnibase_infra.event_bus.kafka_transport.AIOKafkaProducer",
        lambda **_kwargs: producer,
    )
    monkeypatch.setattr(
        "omnibase_infra.cli.cli_ledger.resolve_work_ledger_query_transport",
        lambda _lane: ModelKafkaEventBusConfig(bootstrap_servers="broker.invalid:9092"),
    )
    reader = build_point_read(
        ModelBusSessionReadRoute(kind="bus-session", bus_lane="dev", timeout_seconds=20)
    )
    answer = reader.read(ModelPointQuery(query="rows", lanes=("integx-2-0030",)))
    assert answer.error is None, answer.error
    assert group_ids, group_ids
    assert all(g is None for g in group_ids), group_ids
