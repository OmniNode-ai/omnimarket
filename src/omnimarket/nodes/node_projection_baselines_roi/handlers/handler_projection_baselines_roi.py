# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionBaselinesRoi — project baseline ROI summary to DB.

Consumes onex.evt.omnibase-infra.baselines-computed.v1 and aggregates the
per-pattern comparisons, recommendations, and retry counts into a single
baselines_roi_snapshots row per snapshot_id. The table backs the projection
API topic onex.snapshot.projection.baselines.roi.v1 consumed by the
omnidash BaselinesROICard widget.

Aggregation:
  token_delta   — sum of comparison.token_delta across all comparisons.
  time_delta_ms — sum of comparison.time_delta_s * 1000 across all comparisons.
  retry_delta   — sum of retry_count across all retry_counts.
  recommendations — dict counting each action ("promote", "shadow", "suppress",
                    "fork") across all recommendations.
  confidence    — average mapped score across all comparisons
                  (high→1.0, medium→0.5, low→0.25; 0.0 when no comparisons).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "baselines_roi_snapshots"
CONFLICT_KEY = "snapshot_id"

# OMN-14513 follow-up (tracked separately -- see OMN-14513 PR body): these
# local models encode the SAME fictional wire shape flagged and fixed for
# node_projection_baselines's own consumer path (required-no-default
# pattern_id / token_delta / time_delta_s / confidence / recommendations /
# retry_counts fields the real producer -- omnibase_infra's
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


_CONFIDENCE_SCORES: dict[str, float] = {
    "high": 1.0,
    "medium": 0.5,
    "low": 0.25,
}

_ACTIONS = ("promote", "shadow", "suppress", "fork")


class ModelBaselinesRoiProjectionResult(BaseModel):
    """Result returned by HandlerProjectionBaselinesRoi.project()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionBaselinesRoi:
    """Project baselines-computed events into the baselines_roi_snapshots table."""

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
    ) -> ModelBaselinesRoiProjectionResult:
        """Aggregate event into one baselines_roi_snapshots row and UPSERT."""
        # --- aggregate token delta ---
        token_delta: int = sum(c.token_delta for c in event.comparisons)

        # --- aggregate time delta (seconds → milliseconds) ---
        time_delta_ms: float = sum(c.time_delta_s * 1000.0 for c in event.comparisons)

        # --- aggregate retry delta ---
        retry_delta: int = sum(rc.retry_count for rc in event.retry_counts)

        # --- count recommendation actions ---
        rec_counts: dict[str, int] = dict.fromkeys(_ACTIONS, 0)
        for rec in event.recommendations:
            action = rec.action.lower().strip()
            if action in rec_counts:
                rec_counts[action] += 1

        # --- compute average confidence across comparisons ---
        if event.comparisons:
            total_score = sum(
                _CONFIDENCE_SCORES.get(c.confidence.lower().strip(), 0.0)
                for c in event.comparisons
            )
            confidence: float = total_score / len(event.comparisons)
        else:
            confidence = 0.0

        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "snapshot_id": event.snapshot_id,
            "captured_at": event.computed_at_utc,
            "token_delta": token_delta,
            "time_delta_ms": time_delta_ms,
            "retry_delta": retry_delta,
            "recommendations": rec_counts,
            "confidence": confidence,
            "projected_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelBaselinesRoiProjectionResult(rows_upserted=1)


__all__: list[str] = [
    "HandlerProjectionBaselinesRoi",
    "ModelBaselinesRoiProjectionResult",
]
