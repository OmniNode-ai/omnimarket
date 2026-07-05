# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain coverage for node_projection_live_events.

Proves the reducer's live path: raw platform event -> normalized model ->
database upsert -> projection API exposure contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_live_events.handlers.handler_projection_live_events import (
    HandlerProjectionLiveEvents,
    ModelLiveEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
LOG_TOPIC = "onex.evt.platform.log-entry.v1"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-live-events-applied.v1"
SNAPSHOT_TOPIC = "onex.snapshot.projection.live-events.v1"
TABLE = "live_events"


@pytest.mark.unit
class TestProjectionLiveEventsGoldenChain:
    def test_platform_log_event_materializes_live_event_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        raw_event = {
            "entry_id": str(uuid4()),
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "node_name": "node_build_loop",
            "level": "info",
            "message": "Build loop advanced",
            "correlation_id": "corr-live-001",
        }

        event = ModelLiveEvent.from_raw(raw_event, LOG_TOPIC)
        result = HandlerProjectionLiveEvents().project(event, db)

        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["event_id"] == raw_event["entry_id"]
        assert rows[0]["topic"] == LOG_TOPIC
        assert rows[0]["source"] == "node_build_loop"
        assert rows[0]["summary"] == "Build loop advanced"
        assert rows[0]["correlation_id"] == "corr-live-001"

    def test_contract_exposes_live_events_snapshot(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

        exposures = contract["projection_api"]["exposures"]
        exposure = next(item for item in exposures if item["topic"] == SNAPSHOT_TOPIC)
        assert exposure["table"] == TABLE
        assert "event_id" in exposure["columns"]
        assert LOG_TOPIC in contract["event_bus"]["subscribe_topics"]
        assert contract["terminal_event"] == APPLIED_TOPIC
        assert APPLIED_TOPIC in contract["externally_consumed_topics"]
