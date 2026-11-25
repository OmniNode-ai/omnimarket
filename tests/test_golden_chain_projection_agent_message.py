# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_projection_agent_message: a recorded stream in, one row per record out, every refusal counted and routed.

The stream holds the two synthetic proof messages, a redelivery of one of them, the same offset on a second partition, and one
record each that is dropped, unreadable and refused by the message model. It is replayed twice through the node's ingest handler
and the second pass changes nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_agent_message_kind import EnumAgentMessageKind
from omnimarket.enums.enum_agent_message_platform import EnumAgentMessagePlatform
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_ingest import (
    HandlerAgentMessageIngest,
)
from omnimarket.nodes.node_projection_agent_message.handlers.handler_projection_agent_message import (
    HandlerProjectionAgentMessage,
)
from omnimarket.nodes.node_projection_agent_message.models import (
    ModelAgentMessageDelivery,
    ModelAgentMessageIngestRequest,
    ModelAgentMessageIngestResult,
    ModelAgentMessageRow,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_agent_message/contract.yaml"
)
TOPIC = "onex.evt.omnimarket.agent-message.v1"
BROKER = datetime(2026, 11, 24, 22, 17, 30, tzinfo=UTC)


def _wire(text: str, thread: str = "proof-2217") -> dict[str, object]:
    """The wire form of a message, dumped from the shared message model the sender node publishes."""
    message = ModelAgentMessage.model_validate(
        {
            "from": "claude-h201",
            "to": "codex-h053",
            "thread": thread,
            "kind": EnumAgentMessageKind.MESSAGE,
            "text": text,
            "sent_at": datetime(2026, 11, 24, 22, 17, tzinfo=UTC),
            "platform": EnumAgentMessagePlatform.CLAUDE,
        }
    )
    return message.model_dump(mode="json", by_alias=True)


def _stream() -> list[ModelAgentMessageDelivery]:
    def d(
        partition: int, offset: int, payload: object = None, raw: bytes | None = None
    ) -> ModelAgentMessageDelivery:
        return ModelAgentMessageDelivery(
            topic=TOPIC,
            partition=partition,
            offset=offset,
            broker_time=BROKER,
            payload=payload,
            raw=raw,
        )

    return [
        d(0, 0, _wire("synthetic proof one")),
        d(0, 1, {"payload": {**_wire("synthetic proof two"), "event_id": "e2"}}),
        d(0, 1, {"payload": {**_wire("synthetic proof two"), "event_id": "e2"}}),
        d(1, 0, _wire("other partition", thread="proof-b")),
        d(0, 2),
        d(0, 3, raw=b"\xff\xfe{not json"),
        d(0, 4, {**_wire("bad"), "platform": "nope"}),
    ]


class _Reader:
    def __init__(self, deliveries: list[ModelAgentMessageDelivery]) -> None:
        self._pending = list(deliveries)

    async def read_batch(self, max_count: int) -> list[ModelAgentMessageDelivery]:
        taken, self._pending = self._pending[:max_count], self._pending[max_count:]
        return taken


class _Store:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], ModelAgentMessageRow] = {}

    async def put(self, row: ModelAgentMessageRow) -> bool:
        key = (row.source_partition, row.source_offset)
        if key in self.rows:
            return False
        self.rows[key] = row
        return True


def _replay(store: _Store) -> ModelAgentMessageIngestResult:
    handler = HandlerAgentMessageIngest(reader=_Reader(_stream()), store=store)
    return asyncio.run(handler.handle(ModelAgentMessageIngestRequest()))


def test_the_stream_is_one_row_per_partition_and_offset_with_every_refusal_counted() -> (
    None
):
    store = _Store()
    result = _replay(store)
    assert (result.read, result.projected, result.refused, result.duplicates) == (
        7,
        3,
        3,
        1,
    )
    assert sorted(store.rows) == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (1, 0)]
    reasons = {
        key: row.refusal_reason.value
        for key, row in store.rows.items()
        if row.refusal_reason
    }
    assert reasons == {(0, 2): "dropped", (0, 3): "unreadable", (0, 4): "refused"}


def test_replaying_the_stream_a_second_time_changes_nothing() -> None:
    store = _Store()
    _replay(store)
    before = {k: v.model_dump_json() for k, v in store.rows.items()}
    again = _replay(store)
    assert (again.projected, again.refused, again.duplicates) == (0, 0, 7)
    assert {k: v.model_dump_json() for k, v in store.rows.items()} == before


def test_no_row_of_the_stream_marks_a_sender_verified() -> None:
    store = _Store()
    _replay(store)
    assert [r.sender_verified for r in store.rows.values()] == [False] * 6


def test_a_refused_record_is_routed_to_the_contract_declared_dead_letter_topic() -> (
    None
):
    contract = yaml.safe_load(_CONTRACT.read_text())
    dlq_topics = contract["event_bus"]["dlq_topics"]
    assert len(dlq_topics) == 1
    handler = HandlerProjectionAgentMessage(dlq_topics=dlq_topics)
    refused = [d for d in _stream() if d.offset in (2, 3, 4) and d.partition == 0]
    ok = [d for d in _stream() if d.offset == 0 and d.partition == 0]
    routed = [handler.dlq_envelope(d, handler.handle(d)) for d in refused]
    assert all(r is not None and r[0] == dlq_topics[0] for r in routed)
    record = json.loads(routed[1][1])  # type: ignore[index]
    assert record["partition"] == 0
    assert record["offset"] == 3
    assert record["refusal_reason"] == "unreadable"
    assert handler.dlq_envelope(ok[0], handler.handle(ok[0])) is None
