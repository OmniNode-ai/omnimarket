# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent message projection writes one row per message and counts every message it cannot read."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_ingest import (
    HandlerAgentMessageIngest,
)
from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_tables import (
    AGENT_MESSAGE_COLUMNS,
    insert_statement,
)
from omnimarket.nodes.node_projection_agent_message.handlers.handler_projection_agent_message import (
    HandlerProjectionAgentMessage,
)
from omnimarket.nodes.node_projection_agent_message.models.model_agent_message_delivery import (
    ModelAgentMessageDelivery,
    ModelAgentMessageIngestRequest,
)
from omnimarket.nodes.node_projection_agent_message.models.model_agent_message_row import (
    EnumAgentMessageRefusalReason,
    EnumAgentMessageRowStatus,
    ModelAgentMessageRow,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.omnimarket.agent-message.v1"
BROKER_TIME = datetime(2026, 11, 24, 22, 17, 5, tzinfo=UTC)


def _wire(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "from": "claude-h201",
        "to": "codex-h053",
        "thread": "proof-2217",
        "kind": "message",
        "text": "synthetic proof message",
        "sent_at": "2026-11-24T22:17:00+00:00",
        "platform": "claude",
    }
    base.update(over)
    return base


def _delivery(
    payload: object = None,
    *,
    partition: int = 0,
    offset: int = 0,
    raw: bytes | None = None,
) -> ModelAgentMessageDelivery:
    return ModelAgentMessageDelivery(
        topic=TOPIC,
        partition=partition,
        offset=offset,
        broker_time=BROKER_TIME,
        payload=payload,
        raw=raw,
    )


class _Reader:
    def __init__(self, deliveries: list[ModelAgentMessageDelivery]) -> None:
        self._pending = list(deliveries)

    async def read_batch(self, max_count: int) -> list[ModelAgentMessageDelivery]:
        taken, self._pending = self._pending[:max_count], self._pending[max_count:]
        return taken


class _Store:
    """Keeps the first row for a (partition, offset), as the table's primary key does."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], ModelAgentMessageRow] = {}

    async def put(self, row: ModelAgentMessageRow) -> bool:
        key = (row.source_partition, row.source_offset)
        if key in self.rows:
            return False
        self.rows[key] = row
        return True


def _ingest(deliveries: list[ModelAgentMessageDelivery]) -> tuple[_Store, object]:
    store = _Store()
    handler = HandlerAgentMessageIngest(reader=_Reader(deliveries), store=store)
    result = asyncio.run(handler.handle(ModelAgentMessageIngestRequest(max_count=100)))
    return store, result


def test_a_readable_message_is_one_row_with_every_field() -> None:
    row = HandlerProjectionAgentMessage().handle(
        _delivery(_wire(), partition=2, offset=41)
    )
    assert row.status is EnumAgentMessageRowStatus.OK
    assert row.refusal_reason is None
    assert (row.from_agent, row.to_agent, row.thread) == (
        "claude-h201",
        "codex-h053",
        "proof-2217",
    )
    assert (row.kind, row.platform) == ("message", "claude")
    assert row.text == "synthetic proof message"
    assert row.sent_at == datetime(2026, 11, 24, 22, 17, tzinfo=UTC)
    assert row.broker_at == BROKER_TIME
    assert (row.source_partition, row.source_offset) == (2, 41)


def test_the_enveloped_wire_form_is_read_like_the_bare_one() -> None:
    row = HandlerProjectionAgentMessage().handle(
        _delivery({"payload": {**_wire(), "event_id": "abc"}}, offset=1)
    )
    assert row.status is EnumAgentMessageRowStatus.OK
    assert row.from_agent == "claude-h201"


def test_a_claimed_sender_is_never_marked_verified_before_the_proof_is_deployed() -> (
    None
):
    row = HandlerProjectionAgentMessage().handle(_delivery(_wire(), offset=1))
    assert row.sender_verified is False


def test_a_dropped_message_is_a_counted_row_not_a_gap() -> None:
    store, result = _ingest([_delivery(None, offset=7)])
    assert list(store.rows) == [(0, 7)]
    row = store.rows[(0, 7)]
    assert row.status is EnumAgentMessageRowStatus.REFUSED
    assert row.refusal_reason is EnumAgentMessageRefusalReason.DROPPED
    assert result.refused == 1
    assert result.projected == 0


def test_an_unreadable_message_is_a_counted_row_that_keeps_what_was_read() -> None:
    store, result = _ingest([_delivery(None, offset=8, raw=b"\xff\xfenot json")])
    row = store.rows[(0, 8)]
    assert row.status is EnumAgentMessageRowStatus.REFUSED
    assert row.refusal_reason is EnumAgentMessageRefusalReason.UNREADABLE
    assert row.text is not None
    assert "not json" in row.text
    assert result.refused == 1


def test_a_message_the_model_refuses_is_counted_with_the_reason() -> None:
    bad = _wire(platform="nope")
    store, result = _ingest([_delivery(bad, offset=9)])
    row = store.rows[(0, 9)]
    assert row.status is EnumAgentMessageRowStatus.REFUSED
    assert row.refusal_reason is EnumAgentMessageRefusalReason.REFUSED
    assert row.refusal_detail is not None
    assert "platform" in row.refusal_detail
    assert result.refused == 1


def test_raw_bytes_are_decoded_when_no_payload_was_given() -> None:
    row = HandlerProjectionAgentMessage().handle(
        _delivery(None, offset=3, raw=json.dumps(_wire()).encode())
    )
    assert row.status is EnumAgentMessageRowStatus.OK


def test_the_same_offset_delivered_twice_is_one_row() -> None:
    store, result = _ingest(
        [_delivery(_wire(), offset=5), _delivery(_wire(), offset=5)]
    )
    assert len(store.rows) == 1
    assert result.projected == 1
    assert result.duplicates == 1


def test_the_same_offset_on_two_partitions_is_two_rows() -> None:
    store, result = _ingest(
        [
            _delivery(_wire(text="first"), partition=0, offset=5),
            _delivery(_wire(text="second"), partition=1, offset=5),
        ]
    )
    assert sorted(store.rows) == [(0, 5), (1, 5)]
    assert store.rows[(0, 5)].text == "first"
    assert store.rows[(1, 5)].text == "second"
    assert result.duplicates == 0


def test_the_insert_keeps_the_first_row_for_a_partition_and_offset_in_sql() -> None:
    db = sqlite3.connect(":memory:")
    columns = ", ".join(f'"{c}"' for c in AGENT_MESSAGE_COLUMNS)
    db.execute(
        f'CREATE TABLE agent_messages ({columns}, PRIMARY KEY ("source_partition", "source_offset"))'
    )
    statement = insert_statement().replace("public.", "")
    placeholders = ", ".join("?" for _ in AGENT_MESSAGE_COLUMNS)
    assert "ON CONFLICT" in statement
    assert "DO NOTHING" in statement
    # sqlite uses ? where the statement uses $n
    import re

    sql = re.sub(r"\$\d+", "?", statement)
    assert sql.count("?") == len(AGENT_MESSAGE_COLUMNS) == placeholders.count("?")

    def row(partition: int, offset: int, text: str) -> list[object]:
        values: dict[str, object] = dict.fromkeys(AGENT_MESSAGE_COLUMNS)
        values.update(
            source_partition=partition,
            source_offset=offset,
            status="ok",
            text=text,
            sender_verified=0,
        )
        return [values[c] for c in AGENT_MESSAGE_COLUMNS]

    for args in (
        row(0, 5, "a"),
        row(0, 5, "b"),
        row(1, 5, "c"),
    ):
        db.execute(sql, args)
    stored = db.execute(
        "SELECT source_partition, source_offset, text FROM agent_messages ORDER BY 1, 2"
    ).fetchall()
    assert stored == [(0, 5, "a"), (1, 5, "c")]
