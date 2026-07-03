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

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    ModelBaselinesComputedEvent,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "baselines_roi_snapshots"
CONFLICT_KEY = "snapshot_id"

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
