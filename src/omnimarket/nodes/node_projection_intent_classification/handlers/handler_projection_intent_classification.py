"""HandlerProjectionIntentClassification — project intent-classified events to DB.

Consumes onex.evt.omniintelligence.intent-classified.v1 events and UPSERTs into
the intent_classification_events table. Replay-safe via UPSERT on correlation_id.

Target table schema (from 0000_create_intent_classification_events.sql):
  id            BIGSERIAL PRIMARY KEY
  correlation_id TEXT UNIQUE NOT NULL
  session_id     TEXT NOT NULL
  intent_class   TEXT NOT NULL
  confidence     FLOAT NOT NULL
  keywords       TEXT[]
  emitted_at     TIMESTAMPTZ NOT NULL
  ingested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

# dlq-path-not-required: ValidationError propagates via extra="ignore"; no silent drop.

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "intent_classification_events"
CONFLICT_KEY = "correlation_id"


class ModelIntentClassifiedEvent(BaseModel):
    """Inbound event from onex.evt.omniintelligence.intent-classified.v1.

    Defines only the fields required for the projection UPSERT. Unknown fields
    are silently ignored for forward-compatibility.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Unique correlation identifier.")
    session_id: str = Field(..., description="Session identifier.")
    intent_class: str = Field(
        ..., description="Classified intent class (e.g. feature, bugfix, refactor)."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Classification confidence score."
    )
    keywords: list[str] = Field(default_factory=list, description="Extracted keywords.")
    emitted_at: str | None = Field(
        default=None, description="ISO 8601 emission timestamp."
    )


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionIntentClassification:
    """Project intent-classified events into intent_classification_events table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelIntentClassifiedEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelIntentClassifiedEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelIntentClassifiedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single intent-classification event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "intent_class": event.intent_class,
            "confidence": event.confidence,
            "keywords": event.keywords,
            "emitted_at": event.emitted_at or now,
            "ingested_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelIntentClassifiedEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of intent-classification events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionIntentClassification",
    "ModelIntentClassifiedEvent",
    "ModelProjectionResult",
]
