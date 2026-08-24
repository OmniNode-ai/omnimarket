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
  agent_source   TEXT (nullable; 'claude' | 'cursor' provenance)
"""

# dlq-path-not-required: ValidationError propagates via extra="ignore"; no silent drop.

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from omnimarket.projection.handler_shim import split_projection_input
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "intent_classification_events"
CONFLICT_KEY = "correlation_id"


class ModelIntentClassifiedEvent(BaseModel):
    """Inbound event from onex.evt.omniintelligence.intent-classified.v1.

    Defines only the fields required for the projection UPSERT. Unknown fields
    are silently ignored for forward-compatibility.

    The ``validation_alias`` set is deliberately identical to the key fallbacks
    ``IntentClassificationProjectionRunner.project_event`` already reads
    (``handler_intent_classification.py``). The two paths project the same
    topic into the same table, so a wire key one accepts and the other does not
    is a divergence, not a feature: ``extra="ignore"`` turned an ``agentSource``
    payload into a silent NULL ``agent_source`` on this path while the runner
    persisted it -- the exact silent-drop class OMN-13825 was opened for, on the
    provenance field this epic exists to carry.

    ``timestamp`` aliases ``emitted_at`` for the same reason, and fixes a
    second-order defect: the live publisher names the field ``timestamp``, so
    every real event lost its emission time and ``project()`` substituted
    ``now``, collapsing ``emitted_at`` onto ``ingested_at``.

    Not aliased: the runner's ``_correlation_id`` fallback. Leading-underscore
    wire keys collide with the runtime's injected-metadata contract
    (``projection/handler_shim.py``), and that ambiguity is not worth a
    third-choice fallback no live publisher emits.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    correlation_id: str = Field(
        ...,
        validation_alias=AliasChoices("correlation_id", "correlationId"),
        description="Unique correlation identifier.",
    )
    session_id: str = Field(
        ...,
        validation_alias=AliasChoices("session_id", "sessionId"),
        description="Session identifier.",
    )
    intent_class: str = Field(
        ...,
        # The live publisher (node_claude_hook_event_effect, shared by the
        # Cursor path) emits this field as "intent_category" on the wire;
        # accepting only "intent_class" made every real event fail validation
        # and the projection stay empty.
        validation_alias=AliasChoices("intent_class", "intentClass", "intent_category"),
        description="Classified intent class (e.g. feature, bugfix, refactor).",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Classification confidence score."
    )
    keywords: list[str] = Field(default_factory=list, description="Extracted keywords.")
    emitted_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices("emitted_at", "emittedAt", "timestamp"),
        description="ISO 8601 emission timestamp.",
    )
    agent_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices("agent_source", "agentSource"),
        description=(
            "Originating dispatcher frontend ('claude' | 'cursor'); None for "
            "events published before the field existed."
        ),
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
        db, payload, _meta = split_projection_input(input_data)
        event = ModelIntentClassifiedEvent(**payload)
        result = self.project(event, db)
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
            "agent_source": event.agent_source,
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
