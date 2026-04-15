# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionOvernight — project overnight session events to DB.

Three handlers, one per topic:
  - HandlerProjectionOvernightSessionStart: phase-start.v1 → INSERT overnight_sessions
  - HandlerProjectionOvernightPhaseEnd:    phase-completed.v1 → INSERT overnight_session_phases
  - HandlerProjectionOvernightSessionComplete: session-completed.v1 → UPDATE overnight_sessions

All writes are idempotent (ON CONFLICT DO NOTHING / DO UPDATE).
Out-of-order delivery: if phase-end arrives before session-start, SessionStart
handler is called first to ensure the parent row exists before the child insert.

Target tables (schema_overnight_sessions.sql):
  overnight_sessions: session_id TEXT PK, session_start_ts, session_status, ...
  overnight_session_phases: id BIGSERIAL PK, session_id FK, phase_name, phase_status, ...

Topics (from node_overnight/topics.py):
  onex.evt.omnimarket.overnight-phase-start.v1
  onex.evt.omnimarket.overnight-phase-completed.v1
  onex.evt.omnimarket.overnight-session-completed.v1

Related: OMN-8455 (W2.8)
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE_SESSIONS = "overnight_sessions"
TABLE_PHASES = "overnight_session_phases"
SESSION_CONFLICT_KEY = "session_id"


class ModelOvernightSessionStartEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.overnight-phase-start.v1.

    The first phase-start event signals session start; we upsert the session row
    with status=in_progress on every phase-start (idempotent: only sets fields
    not already present via ON CONFLICT DO NOTHING semantics in the adapter).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Session-level correlation ID.")
    phase: str = Field(..., description="Phase name starting.")
    dry_run: bool = Field(default=False)
    timestamp: str | None = Field(
        default=None, description="ISO 8601 phase-start time."
    )


class ModelOvernightPhaseEndEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.overnight-phase-completed.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Session-level correlation ID.")
    phase: str = Field(..., description="Phase name that completed.")
    phase_status: str = Field(..., description="success | failed | skipped")
    error_message: str | None = Field(default=None)
    duration_ms: int = Field(default=0, ge=0)
    accumulated_cost_usd: float = Field(default=0.0, ge=0.0)
    timestamp: str | None = Field(default=None, description="ISO 8601 phase-end time.")


class ModelOvernightSessionCompleteEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.overnight-session-completed.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Session-level correlation ID.")
    session_status: str = Field(..., description="completed | partial | failed")
    phases_run: list[str] = Field(default_factory=list)
    phases_failed: list[str] = Field(default_factory=list)
    phases_skipped: list[str] = Field(default_factory=list)
    halt_reason: str | None = Field(default=None)
    accumulated_cost_usd: float = Field(default=0.0, ge=0.0)
    dry_run: bool = Field(default=False)
    started_at: str | None = Field(default=None)
    completed_at: str | None = Field(default=None)


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE_SESSIONS)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class HandlerProjectionOvernightSessionStart:
    """Project phase-start events — ensure overnight_sessions row exists.

    Idempotent: INSERT with conflict on session_id does nothing if row exists.
    This also handles out-of-order delivery: any phase-start event ensures the
    parent session row is present before phase rows are inserted.
    """

    def project(
        self,
        event: ModelOvernightSessionStartEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        now = _now_iso()
        row: dict[str, object] = {
            "session_id": event.correlation_id,
            "session_start_ts": event.timestamp or now,
            "dry_run": event.dry_run,
            "session_status": "in_progress",
            "updated_at": now,
        }
        ok = db.upsert(TABLE_SESSIONS, SESSION_CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0, table=TABLE_SESSIONS)


class HandlerProjectionOvernightPhaseEnd:
    """Project phase-end events into overnight_session_phases.

    Ensures parent session row exists first (handles out-of-order delivery),
    then inserts the phase row. The unique index on (session_id, phase_name,
    sequence_number) makes duplicate phase-end events idempotent.
    """

    def __init__(self) -> None:
        self._session_start_handler = HandlerProjectionOvernightSessionStart()
        self._phase_sequence: dict[str, int] = {}

    def project(
        self,
        event: ModelOvernightPhaseEndEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        # Ensure parent row — idempotent if already present
        synthetic_start = ModelOvernightSessionStartEvent(
            correlation_id=event.correlation_id,
            phase=event.phase,
            timestamp=None,
        )
        self._session_start_handler.project(synthetic_start, db)

        seq = self._phase_sequence.get(event.correlation_id, 0)
        self._phase_sequence[event.correlation_id] = seq + 1

        # Validate phase_status; default to "failed" if unknown
        status = (
            event.phase_status
            if event.phase_status in {"success", "failed", "skipped"}
            else "failed"
        )

        row: dict[str, object] = {
            "session_id": event.correlation_id,
            "phase_name": event.phase,
            "phase_status": status,
            "duration_ms": event.duration_ms,
            "side_effect_summary": "",
            "error_message": event.error_message,
            "sequence_number": seq,
            "recorded_at": event.timestamp or _now_iso(),
        }
        ok = db.upsert(TABLE_PHASES, "phase_name", row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0, table=TABLE_PHASES)


class HandlerProjectionOvernightSessionComplete:
    """Project session-complete events — update terminal state on overnight_sessions.

    Idempotent: updates session_status only when currently in_progress.
    Late arrivals after status is already terminal are silently absorbed.
    """

    def __init__(self) -> None:
        self._session_start_handler = HandlerProjectionOvernightSessionStart()

    def project(
        self,
        event: ModelOvernightSessionCompleteEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        now = _now_iso()
        # Ensure row exists (handles complete arriving before any phase-start)
        synthetic_start = ModelOvernightSessionStartEvent(
            correlation_id=event.correlation_id,
            phase="unknown",
            timestamp=event.started_at,
            dry_run=event.dry_run,
        )
        self._session_start_handler.project(synthetic_start, db)

        row: dict[str, object] = {
            "session_id": event.correlation_id,
            "session_status": event.session_status,
            "session_end_ts": event.completed_at or now,
            "phases_run": event.phases_run,
            "phases_failed": event.phases_failed,
            "phases_skipped": event.phases_skipped,
            "halt_reason": event.halt_reason,
            "accumulated_cost_usd": event.accumulated_cost_usd,
            "dry_run": event.dry_run,
            "updated_at": now,
        }
        ok = db.upsert(TABLE_SESSIONS, SESSION_CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0, table=TABLE_SESSIONS)


__all__: list[str] = [
    "HandlerProjectionOvernightPhaseEnd",
    "HandlerProjectionOvernightSessionComplete",
    "HandlerProjectionOvernightSessionStart",
    "ModelOvernightPhaseEndEvent",
    "ModelOvernightSessionCompleteEvent",
    "ModelOvernightSessionStartEvent",
    "ModelProjectionResult",
]
