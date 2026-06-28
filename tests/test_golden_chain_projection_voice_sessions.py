"""Golden chain tests for node_projection_voice_sessions.

Tests cover:
  - session_started: creates a new row with is_active=True, 0 turns
  - session_turn: appends turn, increments count, deduplicates on turn_id
  - session_ended: marks session inactive, sets ended_at
  - full lifecycle: started → multiple turns → ended
  - batch projection
  - event_bus wiring contract assertion
"""

from __future__ import annotations

import json
from typing import Literal

import pytest
import yaml

from omnimarket.nodes.node_projection_voice_sessions.handlers.handler_projection_voice_sessions import (
    HandlerProjectionVoiceSessions,
    ModelTranscriptTurn,
    ModelVoiceSessionEvent,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionVoiceSessions()
PROJECTION_TOPIC = "onex.snapshot.projection.voice.sessions.v1"
TABLE = "voice_sessions"


def _turn(
    turn_id: str, speaker: Literal["user", "agent"] = "user", text: str = "hello"
) -> ModelTranscriptTurn:
    return ModelTranscriptTurn(
        turn_id=turn_id,
        speaker=speaker,
        text=text,
        start_ms=0,
        end_ms=1000,
        confidence=0.95,
    )


class TestSessionStarted:
    def test_creates_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelVoiceSessionEvent(
            session_id="vs-001",
            event_type="session_started",
            started_at="2026-06-28T10:00:00Z",
            agent_name="claude-sonnet-4-6",
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("voice_sessions")
        assert len(rows) == 1
        row = rows[0]
        assert row["session_id"] == "vs-001"
        assert row["is_active"] is True
        assert row["total_turns"] == 0
        assert row["agent_name"] == "claude-sonnet-4-6"
        assert row["ended_at"] is None

    def test_started_at_defaults_to_now_when_absent(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelVoiceSessionEvent(
            session_id="vs-002",
            event_type="session_started",
        )
        HANDLER.project(event, db)
        rows = db.query("voice_sessions")
        assert rows[0]["started_at"] is not None

    def test_agent_name_defaults_to_empty_string(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-003", event_type="session_started"),
            db,
        )
        assert db.query("voice_sessions")[0]["agent_name"] == ""

    def test_empty_transcript_turns_on_start(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-004", event_type="session_started"),
            db,
        )
        raw = db.query("voice_sessions")[0]["transcript_turns"]
        assert json.loads(raw) == []  # type: ignore[arg-type]

    def test_upsert_idempotent_on_double_start(self) -> None:
        db = InmemoryDatabaseAdapter()
        for _ in range(2):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id="vs-005",
                    event_type="session_started",
                    agent_name="agent-a",
                ),
                db,
            )
        rows = db.query("voice_sessions")
        assert len(rows) == 1


class TestSessionTurn:
    def test_appends_turn_and_increments_count(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-010", event_type="session_started"),
            db,
        )
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id="vs-010",
                event_type="session_turn",
                turn=_turn("t-01", "user", "What's the weather?"),
                total_duration_ms=2000,
            ),
            db,
        )
        row = db.query("voice_sessions")[0]
        assert row["total_turns"] == 1
        assert row["total_duration_ms"] == 2000
        turns = json.loads(row["transcript_turns"])  # type: ignore[arg-type]
        assert len(turns) == 1
        assert turns[0]["turn_id"] == "t-01"
        assert turns[0]["speaker"] == "user"

    def test_multiple_turns_accumulate(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-011", event_type="session_started"),
            db,
        )
        for i in range(3):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id="vs-011",
                    event_type="session_turn",
                    turn=_turn(f"t-{i:02d}", "user" if i % 2 == 0 else "agent"),
                    total_duration_ms=(i + 1) * 1000,
                ),
                db,
            )
        row = db.query("voice_sessions")[0]
        assert row["total_turns"] == 3
        assert row["total_duration_ms"] == 3000

    def test_duplicate_turn_id_not_appended(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-012", event_type="session_started"),
            db,
        )
        for _ in range(3):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id="vs-012",
                    event_type="session_turn",
                    turn=_turn("t-dup"),
                ),
                db,
            )
        row = db.query("voice_sessions")[0]
        assert row["total_turns"] == 1

    def test_turn_without_prior_start_creates_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id="vs-013",
                event_type="session_turn",
                turn=_turn("t-01"),
            ),
            db,
        )
        rows = db.query("voice_sessions")
        assert len(rows) == 1

    def test_turn_raises_without_turn_payload(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="session_turn"):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id="vs-014",
                    event_type="session_turn",
                    turn=None,
                ),
                db,
            )

    def test_agent_turn_speaker_stored(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-015", event_type="session_started"),
            db,
        )
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id="vs-015",
                event_type="session_turn",
                turn=_turn("t-a1", "agent", "It's sunny."),
            ),
            db,
        )
        turns = json.loads(db.query("voice_sessions")[0]["transcript_turns"])  # type: ignore[arg-type]
        assert turns[0]["speaker"] == "agent"
        assert turns[0]["text"] == "It's sunny."


class TestSessionEnded:
    def test_marks_session_inactive(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id="vs-020",
                event_type="session_started",
                started_at="2026-06-28T10:00:00Z",
            ),
            db,
        )
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id="vs-020",
                event_type="session_ended",
                ended_at="2026-06-28T10:05:00Z",
                total_duration_ms=300_000,
            ),
            db,
        )
        row = db.query("voice_sessions")[0]
        assert row["is_active"] is False
        assert row["ended_at"] == "2026-06-28T10:05:00Z"
        assert row["total_duration_ms"] == 300_000

    def test_ended_at_defaults_to_now_when_absent(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-021", event_type="session_started"),
            db,
        )
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-021", event_type="session_ended"),
            db,
        )
        row = db.query("voice_sessions")[0]
        assert row["ended_at"] is not None

    def test_ended_without_prior_start_creates_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id="vs-022",
                event_type="session_ended",
                ended_at="2026-06-28T10:05:00Z",
            ),
            db,
        )
        rows = db.query("voice_sessions")
        assert len(rows) == 1
        assert rows[0]["is_active"] is False

    def test_preserves_turn_count_from_prior_turns(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-023", event_type="session_started"),
            db,
        )
        for i in range(4):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id="vs-023",
                    event_type="session_turn",
                    turn=_turn(f"t-{i:02d}"),
                ),
                db,
            )
        HANDLER.project(
            ModelVoiceSessionEvent(session_id="vs-023", event_type="session_ended"),
            db,
        )
        row = db.query("voice_sessions")[0]
        assert row["total_turns"] == 4
        assert row["is_active"] is False


class TestFullLifecycle:
    def test_start_turns_end_sequence(self) -> None:
        db = InmemoryDatabaseAdapter()
        session_id = "vs-100"
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id=session_id,
                event_type="session_started",
                started_at="2026-06-28T09:00:00Z",
                agent_name="claude-sonnet-4-6",
            ),
            db,
        )
        dialogue = [
            ("user", "Hello, what can you do?"),
            ("agent", "I can help with tasks, answer questions, and more."),
            ("user", "Tell me about voice sessions."),
            ("agent", "Voice sessions record spoken turns and project them to a DB."),
        ]
        for idx, (speaker, text) in enumerate(dialogue):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id=session_id,
                    event_type="session_turn",
                    turn=_turn(f"t-{idx:02d}", speaker, text),
                    total_duration_ms=(idx + 1) * 5000,
                ),
                db,
            )
        HANDLER.project(
            ModelVoiceSessionEvent(
                session_id=session_id,
                event_type="session_ended",
                ended_at="2026-06-28T09:01:30Z",
                total_duration_ms=90_000,
            ),
            db,
        )
        rows = db.query("voice_sessions")
        assert len(rows) == 1
        row = rows[0]
        assert row["session_id"] == session_id
        assert row["is_active"] is False
        assert row["total_turns"] == 4
        assert row["total_duration_ms"] == 90_000
        turns = json.loads(row["transcript_turns"])  # type: ignore[arg-type]
        assert len(turns) == 4
        assert turns[0]["speaker"] == "user"
        assert turns[1]["speaker"] == "agent"

    def test_multiple_sessions_isolated(self) -> None:
        db = InmemoryDatabaseAdapter()
        for sess_n in range(3):
            HANDLER.project(
                ModelVoiceSessionEvent(
                    session_id=f"vs-20{sess_n}",
                    event_type="session_started",
                ),
                db,
            )
        assert len(db.query("voice_sessions")) == 3


class TestProjectBatch:
    def test_project_batch_returns_total_count(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelVoiceSessionEvent(
                session_id=f"vs-b{i:02d}",
                event_type="session_started",
            )
            for i in range(5)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 5
        assert len(db.query("voice_sessions")) == 5


class TestContractWiring:
    def test_subscribe_topics_declared(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_voice_sessions/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        topics = contract["event_bus"]["subscribe_topics"]
        assert "onex.evt.omniclaude.voice-session-started.v1" in topics
        assert "onex.evt.omniclaude.voice-session-turn.v1" in topics
        assert "onex.evt.omniclaude.voice-session-ended.v1" in topics

    def test_projection_api_topic_matches_dashboard(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_voice_sessions/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert contract["projection_api"]["topic"] == PROJECTION_TOPIC
        assert contract["projection_api"]["json_columns"] == ["transcript_turns"]

    def test_projection_discovery_registers_voice_sessions_topic(self) -> None:
        topic_map = build_projection_topic_map()

        config = topic_map[PROJECTION_TOPIC]
        assert config.table == TABLE
        assert config.schema_name == "public"
        assert config.json_columns == ("transcript_turns",)

    def test_publish_topics_declared(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_voice_sessions/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert len(contract["event_bus"]["publish_topics"]) >= 1

    def test_db_io_table_declared(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_voice_sessions/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        tables = [t["name"] for t in contract["db_io"]["db_tables"]]
        assert "voice_sessions" in tables


class TestModelValidation:
    def test_extra_fields_ignored(self) -> None:
        event = ModelVoiceSessionEvent.model_validate(
            {
                "session_id": "vs-x",
                "event_type": "session_started",
                "unknown_field": "ignored",
            }
        )
        assert event.session_id == "vs-x"

    def test_transcript_turn_confidence_nullable(self) -> None:
        turn = ModelTranscriptTurn(
            turn_id="t-nc",
            speaker="user",
            text="testing",
            start_ms=0,
            end_ms=500,
            confidence=None,
        )
        assert turn.confidence is None

    def test_invalid_event_type_raises(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="Unknown event_type"):
            HANDLER.project(
                ModelVoiceSessionEvent.model_construct(
                    session_id="vs-err",
                    event_type="bad_type",  # type: ignore[arg-type]
                ),
                db,
            )
