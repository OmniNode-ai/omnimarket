# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work writer through the projection wiring path (OMN-26708).

The database double evaluates the upsert's one rule, ``WHERE seq < EXCLUDED.seq``,
in Python: it keys on the writer's own SQL constants, so a change to the guard in
the SQL text is not seen here. The SQL against a real Postgres is the lab proof.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import pytest

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_lane_work.contract_topics import (
    TOPIC_EXPOSURE_TERMINAL,
    TOPIC_EXPOSURE_WORKFLOW,
    TOPIC_LANE_WORK_TRANSITION,
)
from omnimarket.nodes.node_projection_lane_work.handlers import (
    handler_projection_lane_work as module,
)
from omnimarket.nodes.node_projection_lane_work.handlers.handler_projection_lane_work import (
    LaneWorkProjectionWriter,
)
from tests.unit.nodes.node_projection_lane_work.receipts import (
    blocked_workflow,
    handed_off_workflow,
    live_workflow,
)

pytestmark = pytest.mark.unit


class _FakeDb:
    """One table keyed by workflow id, with the upsert's seq guard."""

    def __init__(self) -> None:
        self.table: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == module._SELECT_ROW:
            row = self.table.get(args[0])
            return None if row is None else json.dumps(row, default=str)
        assert sql == module._UPSERT
        (wid, state, kind, seq, started, last, trans, helps, _dur, tkind, ended, _) = (
            args
        )
        held = self.table.get(wid)
        if held is not None and not held["seq"] < seq:
            return None
        if held is None:
            held = {
                "started_at": started,
                "transitions_taken": 0,
                "help_request_count": 0,
                "terminal_kind": None,
                "ended_at": None,
            }
        started = min(held["started_at"], started)
        self.table[wid] = {
            "workflow_id": wid,
            "state": state,
            "last_event_kind": kind,
            "seq": seq,
            "started_at": started,
            "last_receipt_at": last,
            "transitions_taken": held["transitions_taken"] + trans,
            "help_request_count": held["help_request_count"] + helps,
            "duration_seconds": (last - started).total_seconds(),
            "terminal_kind": tkind or held["terminal_kind"],
            "ended_at": ended or held["ended_at"],
        }
        return wid


def _writer() -> tuple[
    LaneWorkProjectionWriter, _FakeDb, list[tuple[str, dict[str, Any]]]
]:
    writer = LaneWorkProjectionWriter()
    db = _FakeDb()
    cast("Any", writer)._db = db
    published: list[tuple[str, dict[str, Any]]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        published.append((exposure.topic, kwargs["row"]))
        return True

    cast("Any", writer).publish_snapshot_delta = _capture

    async def _no_producer() -> None:
        return None

    cast("Any", writer)._stop_producer = _no_producer
    return writer, db, published


def _deliver(
    writer: LaneWorkProjectionWriter, receipt: Any, offset: int
) -> dict[str, Any]:
    payload = receipt.model_dump(mode="json")
    request = ModelProjectionRequest(
        topic=TOPIC_LANE_WORK_TRANSITION,
        partition=0,
        offset=offset,
        has_coordinates=True,
        fallback_id=f"o{offset}",
        payload=payload,
    )
    return writer.project_request(request)


def test_ac1_the_writer_writes_n_rows_after_n_workflows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer, db, _ = _writer()
    receipts = [
        *live_workflow("a-1-1"),
        *handed_off_workflow("b-2-1"),
        *blocked_workflow("c-3-1"),
    ]
    with caplog.at_level(logging.DEBUG):
        results = [_deliver(writer, r, i) for i, r in enumerate(receipts)]
    assert sorted(db.table) == ["a-1-1", "b-2-1", "c-3-1"]
    assert all(result["rows_upserted"] == 1 for result in results)
    assert "wrote zero rows" not in caplog.text


def test_a_replayed_receipt_writes_zero_rows_and_leaves_the_table_unchanged() -> None:
    writer, db, _ = _writer()
    receipts = handed_off_workflow("b-2-1")
    for i, receipt in enumerate(receipts):
        _deliver(writer, receipt, i)
    before = json.dumps(db.table, default=str, sort_keys=True)
    replayed = [
        _deliver(writer, receipt, i + 100) for i, receipt in enumerate(receipts)
    ]
    assert [r["rows_upserted"] for r in replayed] == [0] * len(receipts)
    assert json.dumps(db.table, default=str, sort_keys=True) == before
    assert db.table["b-2-1"]["help_request_count"] == 1
    assert db.table["b-2-1"]["transitions_taken"] == 8


def test_ac4_only_an_ended_workflow_reaches_the_terminal_exposure() -> None:
    writer, _, published = _writer()
    receipts = [
        *live_workflow("a-1-1"),
        *handed_off_workflow("b-2-1"),
        *blocked_workflow("c-3-1"),
    ]
    for i, receipt in enumerate(receipts):
        _deliver(writer, receipt, i)
    terminal = {
        row["workflow_id"]
        for topic, row in published
        if topic == TOPIC_EXPOSURE_TERMINAL
    }
    current = {
        row["workflow_id"]
        for topic, row in published
        if topic == TOPIC_EXPOSURE_WORKFLOW
    }
    assert terminal == {"b-2-1", "c-3-1"}
    assert current == {"a-1-1", "b-2-1", "c-3-1"}
    assert all(
        row["terminal_kind"] is not None
        for topic, row in published
        if topic == TOPIC_EXPOSURE_TERMINAL
    )
