# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionBaselinesQuality — project baseline quality summary to DB.

Consumes onex.evt.omnibase-infra.baselines-computed.v1 and aggregates the
per-pattern quality signals into a single baselines_quality_snapshots row per
snapshot_id. The table backs the projection API topic
onex.snapshot.projection.baselines.quality.v1 consumed by the omnidash
quality-baseline-panel widget.

Aggregation:
  patterns_compared        — event.patterns_compared (total patterns in window).
  patterns_recommended     — event.patterns_recommended.
  high_confidence_count    — comparisons where confidence == "high".
  medium_confidence_count  — comparisons where confidence == "medium".
  low_confidence_count     — comparisons where confidence == "low".
  quality_score            — weighted average: (high*1.0 + medium*0.5 + low*0.25)
                             / max(1, total_comparisons); 0.0 when no comparisons.
  recommend_rate           — patterns_recommended / max(1, patterns_compared).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "baselines_quality_snapshots"
CONFLICT_KEY = "snapshot_id"

# OMN-14513 follow-up (tracked separately -- see OMN-14513 PR body): these
# local models encode the SAME fictional wire shape flagged and fixed for
# node_projection_baselines's own consumer path (patterns_compared /
# patterns_recommended / confidence fields the real producer -- omnibase_infra's
# ModelBaselinesSnapshotEvent -- never sends). Kept as a private, unchanged
# copy here (rather than re-importing the now-fixed
# handler_projection_baselines module) purely to avoid a compile break: this
# node was importing the OTHER node's now-deleted local model. Behavior is
# byte-for-byte identical to before this PR -- neither newly fixed nor newly
# broken. A follow-up ticket reconciles this node's own aggregation fields
# onto the real producer schema the same way OMN-14513 did for
# node_projection_baselines.


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


_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "high": 1.0,
    "medium": 0.5,
    "low": 0.25,
}


class ModelBaselinesQualityProjectionResult(BaseModel):
    """Result returned by HandlerProjectionBaselinesQuality.project()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionBaselinesQuality:
    """Project baselines-computed events into the baselines_quality_snapshots table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Pops ``_db`` from input_data, constructs a ModelBaselinesComputedEvent,
        delegates to project(), and returns the result as a plain dict.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")

        # Strip runtime-only envelope metadata before constructing the event.
        for meta_key in ("event_landed", "latency_ms", "proof_class"):
            payload.pop(meta_key, None)

        event = ModelBaselinesComputedEvent(**payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelBaselinesComputedEvent,
        db: DatabaseAdapter,
    ) -> ModelBaselinesQualityProjectionResult:
        """Aggregate event into one baselines_quality_snapshots row and UPSERT."""
        total_comparisons = len(event.comparisons)

        # --- confidence tier counts ---
        high_confidence_count: int = 0
        medium_confidence_count: int = 0
        low_confidence_count: int = 0
        weighted_sum: float = 0.0

        for comp in event.comparisons:
            tier = comp.confidence.lower().strip()
            if tier == "high":
                high_confidence_count += 1
            elif tier == "medium":
                medium_confidence_count += 1
            else:
                low_confidence_count += 1
            weighted_sum += _CONFIDENCE_WEIGHTS.get(tier, 0.0)

        # --- weighted quality score ---
        quality_score: float = (
            weighted_sum / total_comparisons if total_comparisons > 0 else 0.0
        )

        # --- recommend rate ---
        recommend_rate: float = (
            event.patterns_recommended / event.patterns_compared
            if event.patterns_compared > 0
            else 0.0
        )

        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "snapshot_id": event.snapshot_id,
            "captured_at": event.computed_at_utc,
            "patterns_compared": event.patterns_compared,
            "patterns_recommended": event.patterns_recommended,
            "high_confidence_count": high_confidence_count,
            "medium_confidence_count": medium_confidence_count,
            "low_confidence_count": low_confidence_count,
            "quality_score": quality_score,
            "recommend_rate": recommend_rate,
            "projected_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelBaselinesQualityProjectionResult(rows_upserted=1)


__all__: list[str] = [
    "HandlerProjectionBaselinesQuality",
    "ModelBaselinesQualityProjectionResult",
]
