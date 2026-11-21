# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One done event is one write, and a malformed event goes to the DLQ (OMN-25104).

- test_done_items_failed_write_keeps_the_held_rows: an event that closes two
  tickets, whose second row's write fails, leaves the first ticket written and
  the Done count wrong until redelivery.
- test_done_items_malformed_event_goes_to_the_contract_dlq: a payload that is
  not a ledger row is retried instead of quarantined when the database does not
  answer, because the store is opened before the payload is read.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    DoneItemsProjectionWriter,
    done_items_from_payload,
)
from tests.nodes.node_projection_done_items.test_done_items_projection import (
    CONTRACT,
    DONE1,
    DONE_TWO,
    V1,
    Payload,
    Served,
    _read,
    _v1,
    served,
)

pytestmark = pytest.mark.unit

__all__ = ["served"]


def _handle(
    writer: DoneItemsProjectionWriter, payload: Payload, offset: int
) -> dict[str, Any]:
    result: dict[str, Any] = writer.handle(
        {**payload, "_topic": V1, "_partition": 0, "_offset": offset}
    )
    return result


async def test_done_items_failed_write_keeps_the_held_rows(
    served: Served, tmp_path: Path
) -> None:
    writer, reader = served
    # The failing ticket must come after one that writes, or a write that is
    # not atomic would never leave a row behind and the test would pass anyway.
    assert [item.ticket for item in done_items_from_payload(_v1(DONE_TWO))] == [
        "OMN-2",
        "OMN-4",
    ]
    _handle(writer, _v1(DONE1), 0)
    held = await _read(reader)

    db = sqlite3.connect(tmp_path / "done.sqlite")
    db.execute(
        "CREATE TRIGGER fail_one BEFORE INSERT ON done_items "
        "WHEN NEW.ticket = 'OMN-4' "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    db.commit()

    with pytest.raises(Exception, match="injected write failure"):
        _handle(writer, _v1(DONE_TWO), 1)

    db.execute("DROP TRIGGER fail_one")
    db.commit()
    db.close()
    assert await _read(reader) == held
    await reader.close()


class _Consumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Message:
    topic = V1
    partition = 0
    offset = 7
    value = json.dumps({"payload": {"not": "a ledger row"}}).encode()


async def test_done_items_malformed_event_goes_to_the_contract_dlq() -> None:
    dlq = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["event_bus"][
        "dlq_topics"
    ]
    writer = DoneItemsProjectionWriter()
    consumer = _Consumer()
    writer._consumer = consumer
    published: list[tuple[str, bytes]] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    writer.publish_dlq = capture  # type: ignore[method-assign]

    await writer._handle_message(_Message())

    assert dlq == ["onex.dlq.omnimarket.projection-done-items-malformed.v1"]
    assert [topic for topic, _ in published] == dlq
    assert json.loads(published[0][1])["original_message"]["not"] == "a ledger row"
    assert [list(offsets.values()) for offsets in consumer.commits] == [[8]]
