# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_session_replay (OMN-13087).

Covers the consume leg:
session lifecycle events -> session_replay_snapshots -> projection API topic.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_session_replay.handlers.handler_projection_session_replay import (
    TOPIC_PROMPT_SUBMITTED,
    TOPIC_SESSION_ENDED,
    TOPIC_SESSION_OUTCOME,
    TOPIC_SESSION_STARTED,
    TOPIC_TOOL_EXECUTED,
    HandlerProjectionSessionReplay,
)
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelSessionReplayEvent,
    ModelSessionReplayState,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSessionReplay()
CONTRACT_PATH = Path(
    "src/omnimarket/nodes/node_projection_session_replay/contract.yaml"
)
MIGRATION_PATH = Path(
    "src/omnimarket/nodes/node_projection_session_replay/migrations/"
    "0001_create_session_replay_snapshots.sql"
)
PROJECTION_TOPIC = "onex.snapshot.projection.session.replay.v1"  # onex-topic-allow: projection snapshot topic asserted against contract discovery
TABLE = "session_replay_snapshots"


class TestSessionReplayProjectionChain:
    def test_row_delta_before_zero_after_one(self) -> None:
        """Row-delta proof: before=0, project one lifecycle event, after=1."""
        db = InmemoryDatabaseAdapter()
        assert len(db.query(TABLE)) == 0

        result = HANDLER.project(
            ModelSessionReplayEvent(
                session_id="sess-golden-001",
                timestamp="2026-06-28T10:00:00Z",
            ),
            db,
            TOPIC_SESSION_STARTED,
        )

        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-golden-001"
        assert rows[0]["sequence"] == 0
        assert rows[0]["event_type"] == "session_start"
        assert rows[0]["is_checkpoint"] is True

    def test_session_lifecycle_materializes_ordered_replay_snapshots(self) -> None:
        db = InmemoryDatabaseAdapter()
        state = ModelSessionReplayState()
        events = [
            (
                TOPIC_SESSION_STARTED,
                ModelSessionReplayEvent(
                    session_id="sess-golden-002",
                    timestamp="2026-06-28T10:00:00Z",
                ),
            ),
            (
                TOPIC_PROMPT_SUBMITTED,
                ModelSessionReplayEvent(
                    session_id="sess-golden-002",
                    timestamp="2026-06-28T10:00:01Z",
                    prompt_preview="summarize current session",
                    tokens_used=12,
                ),
            ),
            (
                TOPIC_TOOL_EXECUTED,
                ModelSessionReplayEvent(
                    session_id="sess-golden-002",
                    timestamp="2026-06-28T10:00:02Z",
                    tool_name="Read",
                    tool_input={"path": "README.md"},
                    tokens_used=8,
                ),
            ),
            (
                TOPIC_SESSION_OUTCOME,
                ModelSessionReplayEvent(
                    session_id="sess-golden-002",
                    timestamp="2026-06-28T10:00:03Z",
                    outcome="success",
                ),
            ),
            (
                TOPIC_SESSION_ENDED,
                ModelSessionReplayEvent(
                    session_id="sess-golden-002",
                    timestamp="2026-06-28T10:00:04Z",
                ),
            ),
        ]

        for topic, event in events:
            state, row = HANDLER.accumulate(state, event, topic)
            db.upsert(TABLE, "snapshot_id", row.model_dump(mode="python"))

        rows = db.query(TABLE)
        assert [row["sequence"] for row in rows] == [0, 1, 2, 3, 4]
        assert [row["event_type"] for row in rows] == [
            "session_start",
            "user_input",
            "tool_call",
            "checkpoint",
            "session_end",
        ]
        assert rows[2]["node_name"] == "Read"
        assert rows[2]["cumulative_tokens"] == 20
        assert state.cumulative_tokens == 20

    def test_handle_materializes_via_injected_db(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(
            {
                "session_id": "sess-golden-003",
                "timestamp": "2026-06-28T10:00:00Z",
                "prompt_preview": "continue",
                "_db": db,
                "_topic": TOPIC_PROMPT_SUBMITTED,
            }
        )

        assert result["rows_upserted"] == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "user_input"
        assert rows[0]["state_delta"] == {"prompt_preview": "continue"}


class TestSessionReplayContractWiring:
    def test_event_bus_subscribes_session_lifecycle_topics(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        assert set(contract["event_bus"]["subscribe_topics"]) == {
            TOPIC_SESSION_STARTED,
            TOPIC_PROMPT_SUBMITTED,
            TOPIC_TOOL_EXECUTED,
            TOPIC_SESSION_OUTCOME,
            TOPIC_SESSION_ENDED,
        }
        assert contract["event_bus"]["publish_topics"]

    def test_projection_api_binds_session_replay_table(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        projection_api = contract["projection_api"]

        assert projection_api["topic"] == PROJECTION_TOPIC
        assert projection_api["table"] == TABLE
        assert projection_api["schema"] == "public"
        assert projection_api["json_columns"] == ["state_delta"]

    def test_projection_discovery_registers_session_replay_topic(self) -> None:
        topic_map = build_projection_topic_map()

        config = topic_map[PROJECTION_TOPIC]
        assert config.table == TABLE
        assert config.schema_name == "public"
        assert config.columns == (
            "snapshot_id",
            "session_id",
            "sequence",
            "timestamp",
            "event_type",
            "node_name",
            "state_delta",
            "cumulative_tokens",
            "is_checkpoint",
        )

    def test_migration_creates_session_replay_snapshots_table(self) -> None:
        sql = MIGRATION_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS public.session_replay_snapshots" in sql
        assert "snapshot_id" in sql
        assert "UNIQUE (session_id, sequence)" in sql
