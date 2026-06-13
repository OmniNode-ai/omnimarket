"""HandlerProjectionPatternLearning — project pattern-stored events to DB.

Consumes onex.evt.omniintelligence.pattern-stored.v1 events and UPSERTs into
the pattern_learning_artifacts table. Dedup by pattern_id (latest-state-wins).

This handler is the consumer the pattern_learning golden chain was missing:
pattern-stored.v1 had no projection consumer and pattern_learning_artifacts was
never written, so the golden-chain sweep timed out (OMN-13124, clears OMN-13102).

Target table schema (see migrations/0000_create_pattern_learning_artifacts.sql):
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  pattern_id UUID UNIQUE NOT NULL
  pattern_name VARCHAR(255) NOT NULL
  pattern_type VARCHAR(100) NOT NULL
  language VARCHAR(50)
  lifecycle_state TEXT NOT NULL DEFAULT 'candidate'
  state_changed_at TIMESTAMPTZ
  composite_score NUMERIC(10, 6) NOT NULL
  scoring_evidence JSONB NOT NULL DEFAULT '{}'
  signature JSONB NOT NULL DEFAULT '{}'
  metrics JSONB DEFAULT '{}'
  metadata JSONB DEFAULT '{}'
  correlation_id TEXT
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "pattern_learning_artifacts"
CONFLICT_KEY = "pattern_id"


class ModelPatternStoredEvent(BaseModel):
    """Inbound event from onex.evt.omniintelligence.pattern-stored.v1.

    Mirrors omniintelligence ModelPatternStoredEvent's wire shape but tolerant of
    the wider projection input surface: extra fields are ignored and only the
    columns the table materializes are required.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    pattern_id: str = Field(..., description="Unique pattern identifier (UPSERT key).")
    pattern_name: str = Field(
        default="",
        description="Human-readable pattern name. Alternate golden-chain lookup key.",
    )
    pattern_type: str = Field(default="", description="Pattern type/category.")
    language: str | None = Field(default=None, description="Source language, if any.")
    lifecycle_state: str = Field(
        default="candidate",
        description="Pattern lifecycle: candidate, promoted, deprecated, archived.",
    )
    state_changed_at: str | None = Field(
        default=None, description="ISO 8601 timestamp of last lifecycle change."
    )
    composite_score: float = Field(
        default=0.0,
        ge=0.0,
        description="Composite quality score used for promotion decisions.",
    )
    scoring_evidence: dict[str, object] = Field(
        default_factory=dict,
        description="JSON evidence used to compute composite_score.",
    )
    signature: dict[str, object] = Field(
        default_factory=dict,
        description="Structural signature of the pattern for similarity matching.",
    )
    metrics: dict[str, object] = Field(
        default_factory=dict, description="Pattern usage/effectiveness metrics."
    )
    metadata: dict[str, object] = Field(
        default_factory=dict, description="Additional pattern metadata."
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for distributed tracing."
    )


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionPatternLearning:
    """Project pattern-stored events into pattern_learning_artifacts table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelPatternStoredEvent and a
        DatabaseAdapter from input_data['_db'].
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        payload.pop("_event_type", None)
        event = ModelPatternStoredEvent(**payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelPatternStoredEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single pattern-stored event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "pattern_id": event.pattern_id,
            "pattern_name": event.pattern_name,
            "pattern_type": event.pattern_type,
            "language": event.language,
            "lifecycle_state": event.lifecycle_state,
            "state_changed_at": event.state_changed_at,
            "composite_score": event.composite_score,
            "scoring_evidence": event.scoring_evidence,
            "signature": event.signature,
            "metrics": event.metrics,
            "metadata": event.metadata,
            "correlation_id": event.correlation_id,
            "updated_at": now,
            "projected_at": now,
        }
        _preserve_existing_evidence(db, row)
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelPatternStoredEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of pattern-stored events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionPatternLearning",
    "ModelPatternStoredEvent",
    "ModelProjectionResult",
]


def _preserve_existing_evidence(
    db: DatabaseAdapter,
    row: dict[str, object],
) -> None:
    """Keep prior evidence when a sparse pattern-stored event arrives later.

    Mirrors node_projection_delegation._preserve_existing_evidence: a later event
    with blank JSON evidence or a zeroed composite_score must not overwrite the
    richer state already materialized for the same pattern_id.
    """
    pattern_id = row.get(CONFLICT_KEY)
    if not pattern_id:
        return
    existing_rows = db.query(TABLE, {CONFLICT_KEY: pattern_id})
    if not existing_rows:
        return
    existing = existing_rows[0]
    for key in ("scoring_evidence", "signature", "metrics", "metadata"):
        if _is_blank_mapping(row.get(key)) and not _is_blank_mapping(existing.get(key)):
            row[key] = existing[key]
    for key in ("pattern_name", "pattern_type", "language", "correlation_id"):
        if _is_blank(row.get(key)) and not _is_blank(existing.get(key)):
            row[key] = existing[key]
    if _is_zero(row.get("composite_score")) and not _is_zero(
        existing.get("composite_score")
    ):
        row["composite_score"] = existing["composite_score"]


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_blank_mapping(value: object) -> bool:
    return value is None or (isinstance(value, dict) and not value)


def _is_zero(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float | Decimal):
        return value == 0
    if isinstance(value, str):
        try:
            return float(value) == 0.0
        except ValueError:
            return False
    return False
