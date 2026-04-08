"""HandlerProjectionBaselines — project baselines-computed events to 4 tables.

Consumes onex.evt.omnibase-infra.baselines-computed.v1 and writes to:
  1. baselines_snapshots (parent row)
  2. baselines_comparisons (per-pattern comparison rows)
  3. baselines_recommendations (per-pattern recommendation rows)
  4. baselines_retry_counts (per-pattern retry count rows)

The write is transactional: snapshot + all children are inserted atomically.
Uses UPSERT on snapshot_id for replay safety.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE_SNAPSHOTS = "baselines_snapshots"
TABLE_COMPARISONS = "baselines_comparisons"
TABLE_RECOMMENDATIONS = "baselines_recommendations"
TABLE_RETRY_COUNTS = "baselines_retry_counts"
CONFLICT_KEY = "snapshot_id"


class ModelBaselinesComparison(BaseModel):
    """A single pattern comparison from the baselines snapshot."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pattern_id: str
    pattern_name: str = ""
    sample_size: int = 0
    window_start: str = ""
    window_end: str = ""
    baseline_tokens: int = 0
    current_tokens: int = 0
    token_delta: int = 0
    token_delta_pct: float = 0.0
    baseline_time_s: float = 0.0
    current_time_s: float = 0.0
    time_delta_s: float = 0.0
    time_delta_pct: float = 0.0
    confidence: str = "low"
    rationale: str = ""


class ModelBaselinesRecommendation(BaseModel):
    """A promotion/demotion recommendation for a pattern."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pattern_id: str
    pattern_name: str = ""
    action: str = ""
    reason: str = ""
    confidence: str = "low"


class ModelBaselinesRetryCount(BaseModel):
    """Retry count for a pattern within the snapshot window."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pattern_id: str
    pattern_name: str = ""
    retry_count: int = 0
    window_start: str = ""
    window_end: str = ""


class ModelBaselinesComputedEvent(BaseModel):
    """Inbound event from onex.evt.omnibase-infra.baselines-computed.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    snapshot_id: str = Field(..., description="Unique snapshot ID.")
    contract_version: int = Field(default=1)
    computed_at_utc: str = Field(..., description="ISO 8601 timestamp.")
    patterns_compared: int = Field(default=0, ge=0)
    patterns_recommended: int = Field(default=0, ge=0)
    comparisons: list[ModelBaselinesComparison] = Field(default_factory=list)
    recommendations: list[ModelBaselinesRecommendation] = Field(default_factory=list)
    retry_counts: list[ModelBaselinesRetryCount] = Field(default_factory=list)


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    tables_written: list[str] = Field(default_factory=list)


class HandlerProjectionBaselines:
    """Project baselines-computed events into 4 baselines tables."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelBaselinesComputedEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelBaselinesComputedEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelBaselinesComputedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a baselines snapshot with all child rows."""
        now = datetime.now(tz=UTC).isoformat()
        rows_total = 0
        tables_written: list[str] = []

        # 1. Snapshot parent row
        snapshot_row: dict[str, object] = {
            "snapshot_id": event.snapshot_id,
            "contract_version": event.contract_version,
            "computed_at_utc": event.computed_at_utc,
            "patterns_compared": event.patterns_compared,
            "patterns_recommended": event.patterns_recommended,
            "projected_at": now,
        }
        if db.upsert(TABLE_SNAPSHOTS, CONFLICT_KEY, snapshot_row):
            rows_total += 1
            tables_written.append(TABLE_SNAPSHOTS)

        # 2. Comparison child rows
        for comp in event.comparisons:
            comp_row: dict[str, object] = {
                "snapshot_id": event.snapshot_id,
                "pattern_id": comp.pattern_id,
                "pattern_name": comp.pattern_name,
                "sample_size": comp.sample_size,
                "window_start": comp.window_start,
                "window_end": comp.window_end,
                "baseline_tokens": comp.baseline_tokens,
                "current_tokens": comp.current_tokens,
                "token_delta": comp.token_delta,
                "token_delta_pct": comp.token_delta_pct,
                "baseline_time_s": comp.baseline_time_s,
                "current_time_s": comp.current_time_s,
                "time_delta_s": comp.time_delta_s,
                "time_delta_pct": comp.time_delta_pct,
                "confidence": comp.confidence,
                "rationale": comp.rationale,
            }
            if db.upsert(TABLE_COMPARISONS, "pattern_id", comp_row):
                rows_total += 1
        if event.comparisons:
            tables_written.append(TABLE_COMPARISONS)

        # 3. Recommendation child rows
        for rec in event.recommendations:
            rec_row: dict[str, object] = {
                "snapshot_id": event.snapshot_id,
                "pattern_id": rec.pattern_id,
                "pattern_name": rec.pattern_name,
                "action": rec.action,
                "reason": rec.reason,
                "confidence": rec.confidence,
            }
            if db.upsert(TABLE_RECOMMENDATIONS, "pattern_id", rec_row):
                rows_total += 1
        if event.recommendations:
            tables_written.append(TABLE_RECOMMENDATIONS)

        # 4. Retry count child rows
        for rc in event.retry_counts:
            rc_row: dict[str, object] = {
                "snapshot_id": event.snapshot_id,
                "pattern_id": rc.pattern_id,
                "pattern_name": rc.pattern_name,
                "retry_count": rc.retry_count,
                "window_start": rc.window_start,
                "window_end": rc.window_end,
            }
            if db.upsert(TABLE_RETRY_COUNTS, "pattern_id", rc_row):
                rows_total += 1
        if event.retry_counts:
            tables_written.append(TABLE_RETRY_COUNTS)

        return ModelProjectionResult(
            rows_upserted=rows_total,
            tables_written=tables_written,
        )


__all__: list[str] = [
    "HandlerProjectionBaselines",
    "ModelBaselinesComputedEvent",
    "ModelProjectionResult",
]
