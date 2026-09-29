# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19977 golden chain: contract, runtime dispatch, durable rows and replay."""

import sqlite3

import pytest
import yaml

from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)
from tests.nodes.node_projection_metering_summary.test_projection import request
from tests.nodes.node_projection_metering_summary.test_writer import (
    NODE,
    SqlRecordingAdapter,
)

pytestmark = pytest.mark.unit

_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-metering-summary-applied.v1"  # onex-topic-allow: this node's declared terminal


def test_the_contract_declares_the_terminal_topic() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert _TERMINAL_TOPIC in contract["event_bus"]["publish_topics"]


def test_the_writer_declares_in_process_dispatch() -> None:
    assert MeteringSummaryProjectionWriter.onex_runtime_inprocess_dispatch is True
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert [
        entry["handler"]["name"] for entry in contract["handler_routing"]["handlers"]
    ] == ["MeteringSummaryProjectionWriter"]
    assert contract["handler"]["class"] == "HandlerProjectionMeteringSummary"


def test_fold_to_writer_to_stored_rows_and_replay() -> None:
    snapshot = request()
    expected = HandlerProjectionMeteringSummary().handle(snapshot).rows
    adapter = SqlRecordingAdapter()
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = adapter  # type: ignore[assignment]
    writer._standalone_bindings = None
    message = snapshot.model_dump(mode="json")
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    message["_topic"] = contract["event_bus"]["subscribe_topics"][0]
    query = "SELECT * FROM metering_summary ORDER BY window_kind, window_start"
    try:
        assert writer.handle(message)["rows_upserted"] == len(expected)
        adapter.connection.row_factory = sqlite3.Row
        stored = [dict(row) for row in adapter.connection.execute(query).fetchall()]
        assert stored == [row.model_dump(mode="json") for row in expected]

        assert writer.handle(message)["rows_upserted"] == len(expected)
        replayed = [dict(row) for row in adapter.connection.execute(query).fetchall()]
        assert replayed == stored
        assert adapter.connects == adapter.closes == 2
    finally:
        adapter.connection.close()
