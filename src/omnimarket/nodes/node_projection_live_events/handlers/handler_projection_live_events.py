"""HandlerProjectionLiveEvents — project platform bus events into live_events table.

Consumes the operator-relevant platform, delegation, and node-generation
topics declared by contract.yaml. The contract is the topic authority; this
handler normalises every declared envelope into one queryable row shape.

UPSERTs into live_events table keyed on event_id.
Projection-API serves /projection/onex.snapshot.projection.live-events.v1.

Target table schema:
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  event_id TEXT UNIQUE NOT NULL
  type TEXT NOT NULL
  timestamp TIMESTAMPTZ NOT NULL
  source TEXT NOT NULL
  topic TEXT NOT NULL
  summary TEXT NOT NULL DEFAULT ''
  payload TEXT NOT NULL DEFAULT ''
  correlation_id TEXT
  created_at TIMESTAMPTZ DEFAULT NOW()
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "live_events"
CONFLICT_KEY = "event_id"
MAX_ROWS = 1000


def _classify_topic(topic: str) -> tuple[str, str]:
    """Derive (event_type, source) from the topic naming convention.

    Topics follow ``onex.evt.<service>.<action>.v<N>`` — all subscribe_topics
    for this node are declared in contract.yaml; no literal topic strings are
    needed here. Classification is derived from the action segment. Ordered
    lifecycle checks keep inference, evaluation, delegation, and routing truth
    distinct instead of collapsing the whole delegation chain into ROUTING.
    """
    parts = topic.split(".")
    # Extract service segment: onex.evt.<service>.*
    service = parts[2] if len(parts) > 2 else "platform"

    topic_lower = topic.lower()
    action = parts[-2].lower() if len(parts) > 1 else ""
    if "failed" in topic_lower or "error" in topic_lower:
        return "ERROR", service
    if ".cmd." in topic_lower:
        return "COMMAND", service
    if action == "routing-decision":
        return "ROUTING", service
    if action == "inference-response":
        return "INFERENCE", service
    if action in {"quality-gate-result", "delegation-judge-verdict"}:
        return "EVALUATION", service
    if (
        "delegation" in action
        or action.startswith("delegate-skill-")
        or action == "task-delegated"
    ):
        return "DELEGATION", service
    if "state-change" in topic_lower or "transformation" in topic_lower:
        return "TRANSFORMATION", service
    return "ACTION", service


def _resolve_event_id(raw: dict[str, Any]) -> str:
    """Return event_id from the raw payload, generating one if absent."""
    for key in ("event_id", "eventId", "id", "entry_id"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(uuid4())


def _resolve_summary(raw: dict[str, Any], topic: str) -> str:
    """Return a human-readable summary line from the raw payload."""
    for key in (
        "summary",
        "message",
        "description",
        "reason",
        "detail",
        "task_description",
        "task_type",
        "status",
    ):
        candidate = raw.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:512]
    return topic


def _resolve_timestamp(raw: dict[str, Any]) -> str:
    """Return ISO 8601 timestamp from payload or generate now()."""
    for key in ("timestamp", "emitted_at", "created_at", "occurred_at"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return datetime.now(tz=UTC).isoformat()


def _resolve_correlation_id(raw: dict[str, Any]) -> str | None:
    """Return correlation_id from the raw payload if present."""
    for key in ("correlation_id", "correlationId", "corr_id"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


class ModelLiveEvent(BaseModel):
    """Normalised live event — the canonical projection input model.

    All fields are derived from the raw platform event payloads so the handler
    can accept any subscribe_topic with a uniform interface.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    event_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Dedup key. Sourced from id/entry_id/event_id in the raw event.",
    )
    type: str = Field(
        default="ACTION",
        description=(
            "Topic-derived event class: COMMAND, ROUTING, INFERENCE, EVALUATION, "
            "DELEGATION, ACTION, TRANSFORMATION, or ERROR."
        ),
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
        description="ISO 8601 timestamp of the source event.",
    )
    source: str = Field(
        default="platform",
        description="Originating service or component.",
    )
    topic: str = Field(..., description="Kafka topic the event was consumed from.")
    summary: str = Field(
        default="",
        description="Human-readable one-line summary of the event.",
    )
    payload: str = Field(
        default="{}",
        description="JSON-serialised raw event payload (stored as text).",
    )
    correlation_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("correlation_id", "correlationId"),
        description="Optional correlation ID for cross-event tracing.",
    )

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        topic: str,
        *,
        envelope_id: UUID | None = None,
    ) -> ModelLiveEvent:
        """Construct from a raw event dict and the source topic string.

        Applies topic-derived defaults for fields not present in the payload
        so every consumer topic maps cleanly to the canonical model.
        """
        topic_type, topic_source = _classify_topic(topic)
        return cls(
            event_id=str(envelope_id)
            if envelope_id is not None
            else _resolve_event_id(raw),
            # The contract-declared source topic owns lifecycle classification.
            # Payload fields named type/event_type are domain data and must not
            # relabel an inference response as routing (or any other class).
            type=topic_type,
            timestamp=_resolve_timestamp(raw),
            source=raw.get("source")
            or raw.get("service_name")
            or raw.get("node_name")
            or topic_source,
            topic=topic,
            summary=_resolve_summary(raw, topic),
            payload=json.dumps(raw, default=str),
            correlation_id=_resolve_correlation_id(raw),
        )


class ModelProjectionResult(BaseModel):
    """Result of a single live-event projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)
    event_id: str = Field(default="")


class HandlerProjectionLiveEvents:
    """Project platform bus events into the live_events table.

    All projection methods are pure with respect to I/O — the DatabaseAdapter
    is injected. No network calls, no Kafka I/O in this class.
    """

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Expects input_data to contain:
          - ``_db``: DatabaseAdapter instance
          - ``_topic``: source Kafka topic string
          - All other keys: raw event payload fields
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")

        topic = input_data.pop("_topic", "")
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError(
                "handle() requires input_data['_topic'] as a non-empty string"
            )

        envelope_id = input_data.pop("_envelope_id", None)
        if envelope_id is not None and not isinstance(envelope_id, UUID):
            raise TypeError("input_data['_envelope_id'] must be a UUID when present")

        raw: dict[str, Any] = dict(input_data)
        event = ModelLiveEvent.from_raw(raw, topic, envelope_id=envelope_id)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelLiveEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a live event row into the live_events table."""
        existing_rows = db.query(TABLE, {CONFLICT_KEY: event.event_id})
        existing_created_at = (
            existing_rows[0].get("created_at") if existing_rows else None
        )
        row: dict[str, object] = {
            "event_id": event.event_id,
            "type": event.type,
            "timestamp": event.timestamp,
            "source": event.source,
            "topic": event.topic,
            "summary": event.summary,
            "payload": event.payload,
            "correlation_id": event.correlation_id,
            "created_at": existing_created_at or datetime.now(tz=UTC).isoformat(),
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(
            rows_upserted=1 if ok else 0,
            event_id=event.event_id,
        )


__all__: list[str] = [
    "CONFLICT_KEY",
    "MAX_ROWS",
    "TABLE",
    "HandlerProjectionLiveEvents",
    "ModelLiveEvent",
    "ModelProjectionResult",
]
