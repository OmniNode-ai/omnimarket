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

Dispatch entrypoint (OMN-14802, canonical def-B): each handler exposes a
``handle(event) -> dict`` that OWNS the projection behavior — it coerces the
inbound event (a validated model from the RuntimeLocal event-bus path, or a raw
payload mapping from the production Kafka auto-wiring), resolves the
``DatabaseAdapter``, performs the UPSERT, and returns the serialized result. The
shared runtime binds this method (``omnibase_infra`` ``handler_wiring``
``_make_dispatch_callback`` and ``omnibase_core`` ``LocalRuntimeBusAdapter``);
before this entrypoint existed all three handlers were bound to ``_missing_handle``
/ hit a bare ``AttributeError`` on every real dispatch and were frozen into
``validation/handler_dispatch_entrypoint_baseline.yaml`` (OMN-14617 ratchet). The
pure ``(event, db) -> ModelProjectionResult`` reducer body is retained as a
private ``_project`` core; ``project(event, db)`` stays as a thin typed
backward-compat wrapper for the existing golden-chain unit tests.

Target tables (schema_overnight_sessions.sql):
  overnight_sessions: session_id TEXT PK, session_start_ts, session_status, ...
  overnight_session_phases: id BIGSERIAL PK, session_id FK, phase_name, phase_status, ...

Topics (from node_overnight/topics.py):
  onex.evt.omnimarket.overnight-phase-start.v1
  onex.evt.omnimarket.overnight-phase-completed.v1
  onex.evt.omnimarket.overnight-session-completed.v1

Related: OMN-8455 (W2.8), OMN-14802 (def-B dispatch entrypoint)
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
)

TABLE_SESSIONS = "overnight_sessions"
TABLE_PHASES = "overnight_session_phases"
SESSION_CONFLICT_KEY = "session_id"

# Transport/materialization keys the auto-wiring layer may fold into the payload
# mapping alongside the domain fields; stripped before event construction (mirrors
# node_projection_savings' handle() strip-set).
_TRANSPORT_KEYS = frozenset({"rows", "event_landed", "latency_ms"})


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


def _domain_fields(payload: Mapping[str, object]) -> dict[str, object]:
    """Drop dispatch transport keys (``_db``/``_event_type``/``_topic``/…) before
    constructing the typed domain event."""
    return {
        key: value
        for key, value in payload.items()
        if not key.startswith("_") and key not in _TRANSPORT_KEYS
    }


def _resolve_db(payload: Mapping[str, object]) -> DatabaseAdapter:
    """Resolve the ``DatabaseAdapter`` for a def-B dispatch payload mapping.

    The production Kafka auto-wiring path injects the real adapter as
    ``input_data['_db']`` (``handler_wiring`` line ~2031), so a production dispatch
    always UPSERTs into Postgres. The event-driven RuntimeLocal path forwards the
    decoded wire dict with no ``_db`` (the contract's ``event_model`` is a dotted
    string, so the bus adapter does not pre-validate/enrich it); there an owned
    in-memory adapter is used so the projection still executes end-to-end — the
    dispatch-proof default (COMPLETED + a row actually written, not merely
    "dispatch resolved"). This default only triggers when ``_db`` is absent; a
    ``_db`` present but of the wrong type fails loud rather than silently dropping
    a real production adapter.
    """
    db = payload.get("_db")
    if db is None:
        return InmemoryDatabaseAdapter()
    if not isinstance(db, DatabaseAdapter):
        raise TypeError(
            "def-B handle() received input_data['_db'] that is not a "
            f"DatabaseAdapter (got {type(db).__name__})"
        )
    return db


class HandlerProjectionOvernightSessionStart:
    """Project phase-start events — ensure overnight_sessions row exists.

    Idempotent: INSERT with conflict on session_id does nothing if row exists.
    This also handles out-of-order delivery: any phase-start event ensures the
    parent session row is present before phase rows are inserted.
    """

    def handle(self, event: object) -> dict[str, object]:
        """Canonical def-B dispatch entrypoint — owns the phase-start projection.

        ``event`` is a raw payload mapping on both live dispatch paths: the
        production Kafka auto-wiring carries the injected ``_db``, while the
        event-driven RuntimeLocal path forwards the decoded wire dict with no
        ``_db`` (so the projection writes into an owned in-memory adapter — the
        dispatch-proof default). An already-validated ``ModelOvernightSessionStartEvent``
        is also accepted (a payload_type_match / map-form ``event_model`` contract).
        """
        parsed, db = self._coerce(event)
        return self._project(parsed, db).model_dump(mode="json")

    @staticmethod
    def _coerce(
        event: object,
    ) -> tuple[ModelOvernightSessionStartEvent, DatabaseAdapter]:
        if isinstance(event, ModelOvernightSessionStartEvent):
            return event, InmemoryDatabaseAdapter()
        if isinstance(event, Mapping):
            return (
                ModelOvernightSessionStartEvent(**_domain_fields(event)),
                _resolve_db(event),
            )
        raise TypeError(
            "HandlerProjectionOvernightSessionStart.handle() expected "
            "ModelOvernightSessionStartEvent or a payload mapping, got "
            f"{type(event).__name__}"
        )

    def _project(
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

    def project(
        self,
        event: ModelOvernightSessionStartEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Backward-compat typed reducer surface (golden-chain unit tests)."""
        return self._project(event, db)


class HandlerProjectionOvernightPhaseEnd:
    """Project phase-end events into overnight_session_phases.

    Ensures parent session row exists first (handles out-of-order delivery),
    then inserts the phase row. The unique index on (session_id, phase_name,
    sequence_number) makes duplicate phase-end events idempotent.
    """

    def __init__(
        self,
        session_start_handler: HandlerProjectionOvernightSessionStart | None = None,
    ) -> None:
        self._session_start_handler = (
            session_start_handler
            if session_start_handler is not None
            else HandlerProjectionOvernightSessionStart()
        )
        self._phase_sequence: dict[str, int] = {}

    def handle(self, event: object) -> dict[str, object]:
        """Canonical def-B dispatch entrypoint — owns the phase-end projection."""
        parsed, db = self._coerce(event)
        return self._project(parsed, db).model_dump(mode="json")

    @staticmethod
    def _coerce(
        event: object,
    ) -> tuple[ModelOvernightPhaseEndEvent, DatabaseAdapter]:
        if isinstance(event, ModelOvernightPhaseEndEvent):
            return event, InmemoryDatabaseAdapter()
        if isinstance(event, Mapping):
            return (
                ModelOvernightPhaseEndEvent(**_domain_fields(event)),
                _resolve_db(event),
            )
        raise TypeError(
            "HandlerProjectionOvernightPhaseEnd.handle() expected "
            "ModelOvernightPhaseEndEvent or a payload mapping, got "
            f"{type(event).__name__}"
        )

    def _project(
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

    def project(
        self,
        event: ModelOvernightPhaseEndEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Backward-compat typed reducer surface (golden-chain unit tests)."""
        return self._project(event, db)


class HandlerProjectionOvernightSessionComplete:
    """Project session-complete events — update terminal state on overnight_sessions.

    Idempotent: updates session_status only when currently in_progress.
    Late arrivals after status is already terminal are silently absorbed.
    """

    def __init__(
        self,
        session_start_handler: HandlerProjectionOvernightSessionStart | None = None,
    ) -> None:
        self._session_start_handler = (
            session_start_handler
            if session_start_handler is not None
            else HandlerProjectionOvernightSessionStart()
        )

    def handle(self, event: object) -> dict[str, object]:
        """Canonical def-B dispatch entrypoint — owns the session-complete projection."""
        parsed, db = self._coerce(event)
        return self._project(parsed, db).model_dump(mode="json")

    @staticmethod
    def _coerce(
        event: object,
    ) -> tuple[ModelOvernightSessionCompleteEvent, DatabaseAdapter]:
        if isinstance(event, ModelOvernightSessionCompleteEvent):
            return event, InmemoryDatabaseAdapter()
        if isinstance(event, Mapping):
            return (
                ModelOvernightSessionCompleteEvent(**_domain_fields(event)),
                _resolve_db(event),
            )
        raise TypeError(
            "HandlerProjectionOvernightSessionComplete.handle() expected "
            "ModelOvernightSessionCompleteEvent or a payload mapping, got "
            f"{type(event).__name__}"
        )

    def _project(
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

    def project(
        self,
        event: ModelOvernightSessionCompleteEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Backward-compat typed reducer surface (golden-chain unit tests)."""
        return self._project(event, db)


__all__: list[str] = [
    "HandlerProjectionOvernightPhaseEnd",
    "HandlerProjectionOvernightSessionComplete",
    "HandlerProjectionOvernightSessionStart",
    "ModelOvernightPhaseEndEvent",
    "ModelOvernightSessionCompleteEvent",
    "ModelOvernightSessionStartEvent",
    "ModelProjectionResult",
]
