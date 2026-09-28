# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_lab_container_memory`` (OMN-19961).

Walks the chain the contract declares, hop by hop, with task 5's own event
fixture (two records on host ``omnipc2``):

    onex.evt.omnibase-infra.lane-container-memory.v1   (one event per host per pass)
        -> lab_container_memory_window                 (one row per container per window)
        -> onex.evt.omnimarket.projection-lab-container-memory-applied.v1  (terminal)

and the error leg:

    a record without peak_bytes
        -> onex.dlq.omnimarket.projection-lab-container-memory-malformed.v1

Each hop is driven through the entry the runtime actually calls: the writer's
synchronous ``handle()`` on the in-process projection arm, whose return value
the runtime's write-path guard reads (``rows_upserted``) before it emits the
terminal event.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_writer import (
    TABLE,
    LabContainerMemoryProjectionWriter,
)
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_container_memory"
)
_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "lab_container_memory"
    / "event.v1.json"
)
_EVENT_TOPIC = "onex.evt.omnibase-infra.lane-container-memory.v1"  # onex-topic-allow: the producer's declared topic, omnibase_infra side of OMN-19959
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-lab-container-memory-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-lab-container-memory-malformed.v1"  # onex-topic-allow: this node's declared DLQ


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (_NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def _event() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return loaded


class _Table:
    """The table, keyed on its primary key, behind the adapter's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[Any, ...]] = {}
        self.open = False

    async def connect(self) -> None:
        self.open = True

    async def close(self) -> None:
        self.open = False

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self.open, "the writer wrote outside its own connection bracket"
        assert f"INSERT INTO {TABLE}" in query
        self.rows[str(params[0])] = params
        return [{"record_key": params[0]}]


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> LabContainerMemoryProjectionWriter:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    instance = LabContainerMemoryProjectionWriter()
    instance._db = _Table()  # type: ignore[assignment]
    return instance


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [_EVENT_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["event_bus"]["publish_topics"] == [_TERMINAL_TOPIC]
    assert contract["event_bus"]["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["db_io"]["db_tables"][0]["name"] == TABLE.split(".", 1)[1]


def test_golden_chain_event_to_rows_to_terminal(
    writer: LabContainerMemoryProjectionWriter,
) -> None:
    table: _Table = writer._db  # type: ignore[assignment]
    event = _event()
    message = copy.deepcopy(event)
    message["_topic"] = _EVENT_TOPIC

    applied = writer.handle(message)

    # Hop 2: one row per record, under the producer's key.
    assert set(table.rows) == {r["record_key"] for r in event["records"]}
    # Hop 3: the payload the runtime's write-path guard reads before it emits
    # the terminal. Zero here would suppress the terminal for a real write.
    assert applied["rows_upserted"] == 2
    assert applied["host"] == "omnipc2"
    assert applied["window_end"].startswith("2026-09-28T19:00:00")


def test_golden_chain_error_leg_routes_a_missing_peak_to_the_dlq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    published: list[str] = []

    async def _publish(topic: str, value: bytes) -> None:
        published.append(topic)

    writer = LabContainerMemoryProjectionWriter(publish_fn=_publish)
    table = _Table()
    writer._db = table  # type: ignore[assignment]
    event = _event()
    del event["records"][0]["peak_bytes"]
    meta = MessageMeta(partition=0, offset=3, fallback_id="gc", topic=_EVENT_TOPIC)

    with pytest.raises(ValidationError) as caught:
        writer.handle({**copy.deepcopy(event), "_topic": _EVENT_TOPIC})
    assert classify_projection_error(caught.value) is ProjectionErrorClass.POISON

    routed = asyncio.run(
        writer._route_poison_to_dlq(_EVENT_TOPIC, event, caught.value, meta)
    )
    assert routed is True
    assert published == [_DLQ_TOPIC]
    assert table.rows == {}
