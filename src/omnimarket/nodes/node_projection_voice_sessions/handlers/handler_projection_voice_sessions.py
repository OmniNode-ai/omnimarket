"""HandlerProjectionVoiceSessions — project voice session events to DB.

Consumes three event types from omniclaude:
  - onex.evt.omniclaude.voice-session-started.v1
  - onex.evt.omniclaude.voice-session-turn.v1
  - onex.evt.omniclaude.voice-session-ended.v1

UPSERTs into the voice_sessions table keyed on session_id.
Replay-safe: all operations are idempotent via UPSERT.

Target table schema (migration 0001_create_voice_sessions.sql):
  session_id          TEXT PRIMARY KEY
  started_at          TIMESTAMPTZ NOT NULL
  ended_at            TIMESTAMPTZ
  is_active           BOOLEAN NOT NULL DEFAULT TRUE
  total_turns         INTEGER NOT NULL DEFAULT 0
  total_duration_ms   BIGINT NOT NULL DEFAULT 0
  agent_name          TEXT NOT NULL DEFAULT ''
  transcript_turns    JSONB NOT NULL DEFAULT '[]'
  ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "voice_sessions"
CONFLICT_KEY = "session_id"

EventType = Literal["session_started", "session_turn", "session_ended"]


class ModelTranscriptTurn(BaseModel):
    """A single transcript turn within a voice session."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    turn_id: str = Field(..., description="Unique identifier for this turn.")
    speaker: Literal["user", "agent"] = Field(..., description="Who spoke.")
    text: str = Field(..., description="Transcript text for this turn.")
    start_ms: int = Field(..., ge=0, description="Turn start time in milliseconds.")
    end_ms: int = Field(..., ge=0, description="Turn end time in milliseconds.")
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="ASR confidence score [0,1] or None if unavailable.",
    )


class ModelVoiceSessionEvent(BaseModel):
    """Inbound event envelope for all voice session event types.

    ``event_type`` discriminates the handler path:
    - ``session_started``: emitted by onex.evt.omniclaude.voice-session-started.v1
    - ``session_turn``:    emitted by onex.evt.omniclaude.voice-session-turn.v1
    - ``session_ended``:   emitted by onex.evt.omniclaude.voice-session-ended.v1
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(..., description="Unique session identifier.")
    event_type: EventType = Field(..., description="Discriminator for handler routing.")

    # session_started fields
    started_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp when session started.",
    )
    agent_name: str | None = Field(
        default=None,
        description="Name of the agent driving the session.",
    )

    # session_turn fields
    turn: ModelTranscriptTurn | None = Field(
        default=None,
        description="Transcript turn appended on session_turn events.",
    )
    total_duration_ms: int | None = Field(
        default=None,
        ge=0,
        description="Cumulative duration in ms (updated per turn or on end).",
    )

    # session_ended fields
    ended_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp when session ended.",
    )


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)
    event_type: str = Field(default="")


class HandlerProjectionVoiceSessions:
    """Project voice session events into the voice_sessions table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelVoiceSessionEvent and a
        DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelVoiceSessionEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelVoiceSessionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Route to the appropriate projection based on event_type."""
        if event.event_type == "session_started":
            return self._handle_started(event, db)
        if event.event_type == "session_turn":
            return self._handle_turn(event, db)
        if event.event_type == "session_ended":
            return self._handle_ended(event, db)
        raise ValueError(f"Unknown event_type: {event.event_type!r}")

    # ------------------------------------------------------------------
    # Internal per-event-type handlers
    # ------------------------------------------------------------------

    def _handle_started(
        self,
        event: ModelVoiceSessionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a new voice session row on session_started."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "session_id": event.session_id,
            "started_at": event.started_at or now,
            "ended_at": None,
            "is_active": True,
            "total_turns": 0,
            "total_duration_ms": 0,
            "agent_name": event.agent_name or "",
            "transcript_turns": json.dumps([]),
            "ingested_at": now,
            "updated_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1, event_type="session_started")

    def _handle_turn(
        self,
        event: ModelVoiceSessionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Append a transcript turn and update counters."""
        if event.turn is None:
            raise ValueError("session_turn event must include a 'turn' payload")
        now = datetime.now(tz=UTC).isoformat()

        # Fetch current row to merge turn list and counters.
        existing = db.query(TABLE, {"session_id": event.session_id})
        if existing:
            current = existing[0]
            existing_turns_raw = current.get("transcript_turns", "[]")
            if isinstance(existing_turns_raw, str):
                existing_turns: list[Any] = json.loads(existing_turns_raw)
            else:
                existing_turns = []
            raw_count = current.get("total_turns", 0)
            current_count = int(raw_count) if isinstance(raw_count, (int, str)) else 0
            started_at = current.get("started_at", now)
            agent_name = current.get("agent_name", "")
        else:
            existing_turns = []
            current_count = 0
            started_at = now
            agent_name = ""

        # Append the new turn (dedup by turn_id).
        turn_dict = event.turn.model_dump(mode="json")
        turn_ids = {t["turn_id"] for t in existing_turns if isinstance(t, dict)}
        if turn_dict["turn_id"] not in turn_ids:
            existing_turns.append(turn_dict)
            new_count = current_count + 1
        else:
            new_count = current_count

        row: dict[str, object] = {
            "session_id": event.session_id,
            "started_at": started_at,
            "ended_at": None,
            "is_active": True,
            "total_turns": new_count,
            "total_duration_ms": event.total_duration_ms or 0,
            "agent_name": agent_name,
            "transcript_turns": json.dumps(existing_turns),
            "ingested_at": now,
            "updated_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1, event_type="session_turn")

    def _handle_ended(
        self,
        event: ModelVoiceSessionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Mark the session inactive on session_ended."""
        now = datetime.now(tz=UTC).isoformat()

        existing = db.query(TABLE, {"session_id": event.session_id})
        if existing:
            current = existing[0]
            started_at = current.get("started_at", now)
            raw_turns = current.get("total_turns", 0)
            total_turns = int(raw_turns) if isinstance(raw_turns, (int, str)) else 0
            agent_name = current.get("agent_name", "")
            transcript_turns = current.get("transcript_turns", "[]")
            raw_dur = current.get("total_duration_ms", 0)
            stored_duration = int(raw_dur) if isinstance(raw_dur, (int, str)) else 0
            duration_ms = (
                event.total_duration_ms
                if event.total_duration_ms is not None
                else stored_duration
            )
        else:
            started_at = now
            total_turns = 0
            agent_name = ""
            transcript_turns = json.dumps([])
            duration_ms = event.total_duration_ms or 0

        row: dict[str, object] = {
            "session_id": event.session_id,
            "started_at": started_at,
            "ended_at": event.ended_at or now,
            "is_active": False,
            "total_turns": total_turns,
            "total_duration_ms": duration_ms,
            "agent_name": agent_name,
            "transcript_turns": transcript_turns,
            "ingested_at": now,
            "updated_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1, event_type="session_ended")

    def project_batch(
        self,
        events: list[ModelVoiceSessionEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Project a batch of voice session events in order."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "EventType",
    "HandlerProjectionVoiceSessions",
    "ModelProjectionResult",
    "ModelTranscriptTurn",
    "ModelVoiceSessionEvent",
]
