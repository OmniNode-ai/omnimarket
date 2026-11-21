# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One integration pass is one write: whole, or not at all (OMN-25103).

The writer holds the newest pass. These tests pin the edges of that: a pass
whose write fails part-way leaves the held pass as it was, a pass with the
same as_of as the held one changes nothing, an empty newer pass leaves no
rows, and a malformed pass goes to the contract's DLQ topic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_floor_integration.handlers.handler_floor_integration_writer import (
    FloorIntegrationProjectionWriter,
)
from tests.nodes.node_integration_reading_publish_effect.test_integration_reading_publish import (
    _batch,
)
from tests.nodes.node_projection_floor_integration.test_floor_integration_projection import (
    CONTRACT,
    EVENT,
    Payload,
    Served,
    _project,
    _read,
    served,
)

pytestmark = pytest.mark.unit

__all__ = ["served"]


def _handle(
    writer: FloorIntegrationProjectionWriter, envelope: Payload, offset: int
) -> dict[str, Any]:
    result: dict[str, Any] = writer.handle(
        {**envelope, "_topic": EVENT, "_partition": 0, "_offset": offset}
    )
    return result


def _newer(hours: int, readings: list[Any] | None = None) -> Any:
    batch = _batch()
    update: dict[str, Any] = {"as_of": batch.as_of + timedelta(hours=hours)}
    if readings is not None:
        update["readings"] = readings
    return batch.model_copy(update=update)


async def test_floor_integration_failed_write_keeps_the_held_pass(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader = served
    _, first = _project(_batch())
    _handle(writer, first, 0)
    held = await _read(reader)

    projection, second = _project(_newer(1))
    last_key = list(projection.rows)[-1]
    assert len(projection.rows) > 1, "the pass must write rows before the failing one"
    db = sqlite3.connect(tmp_path / "floor.sqlite")
    db.execute(
        "CREATE TRIGGER fail_one BEFORE UPDATE ON floor_integration_rows "
        f"WHEN NEW.key = '{last_key}' "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    db.commit()

    deltas: list[Any] = []

    async def record(*args: Any, **kwargs: Any) -> None:
        deltas.append((args, kwargs))

    monkeypatch.setattr(writer, "publish_snapshot_delta", record)
    with pytest.raises(Exception, match="injected write failure"):
        _handle(writer, second, 1)
    # Nothing is published for a pass the table does not hold.
    assert deltas == []

    db.execute("DROP TRIGGER fail_one")
    db.commit()
    db.close()
    assert await _read(reader) == held
    await reader.close()


async def test_floor_integration_equal_as_of_pass_changes_nothing(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader = served
    _, envelope = _project(_batch())
    _handle(writer, envelope, 0)
    first = await _read(reader)

    deltas: list[Any] = []

    async def record(*args: Any, **kwargs: Any) -> None:
        deltas.append((args, kwargs))

    monkeypatch.setattr(writer, "publish_snapshot_delta", record)
    result = _handle(writer, envelope, 1)

    assert result["rows_upserted"] == 0
    assert result["rows_deleted"] == 0
    assert await _read(reader) == first
    assert deltas == []
    await reader.close()


async def test_floor_integration_same_as_of_with_corrected_content_is_applied(
    served: Served,
) -> None:
    """A pass re-sent with the held as_of but corrected content replaces the held rows."""
    writer, reader = served
    batch = _batch()
    _, first = _project(batch)
    _handle(writer, first, 0)

    corrected_reading = batch.readings[0].model_copy(
        update={"title": "corrected title"}
    )
    corrected = batch.model_copy(
        update={"readings": [corrected_reading, *batch.readings[1:]]}
    )
    projection, envelope = _project(corrected)
    keys = [
        key for key, row in projection.rows.items() if row.title == "corrected title"
    ]
    assert len(keys) == 1
    result = _handle(writer, envelope, 1)

    assert result["rows_upserted"] >= 1
    assert (await _read(reader))[keys[0]]["title"] == "corrected title"
    await reader.close()


async def test_floor_integration_same_as_of_without_a_key_removes_it(
    served: Served,
) -> None:
    """A corrected pass with the held as_of that no longer names a change drops its row."""
    writer, reader = served
    batch = _batch()
    full, first = _project(batch)
    _handle(writer, first, 0)

    corrected, envelope = _project(
        batch.model_copy(update={"readings": batch.readings[1:]})
    )
    dropped = set(full.rows) - set(corrected.rows)
    assert len(dropped) == 1
    result = _handle(writer, envelope, 1)

    assert set(await _read(reader)) == set(corrected.rows)
    assert result["rows_deleted"] == 1
    await reader.close()


async def test_floor_integration_empty_newer_pass_clears_the_rows(
    served: Served,
) -> None:
    writer, reader = served
    _, first = _project(_batch())
    _handle(writer, first, 0)
    assert await _read(reader)

    _, empty = _project(_newer(1, readings=[]))
    result = _handle(writer, empty, 1)

    assert await _read(reader) == {}
    assert result["rows_upserted"] == 0
    assert result["rows_deleted"] > 0
    await reader.close()


class _Consumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Message:
    topic = EVENT
    partition = 0
    offset = 7
    value = json.dumps({"payload": {"not": "an envelope"}}).encode()


async def test_floor_integration_malformed_pass_goes_to_the_contract_dlq() -> None:
    dlq = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["event_bus"][
        "dlq_topics"
    ]
    writer = FloorIntegrationProjectionWriter()
    consumer = _Consumer()
    writer._consumer = consumer
    published: list[tuple[str, bytes]] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    writer.publish_dlq = capture  # type: ignore[method-assign]

    await writer._handle_message(_Message())

    assert dlq == ["onex.dlq.omnimarket.projection-floor-integration-malformed.v1"]
    assert [topic for topic, _ in published] == dlq
    assert json.loads(published[0][1])["original_message"]["not"] == "an envelope"
    assert [list(offsets.values()) for offsets in consumer.commits] == [[8]]
