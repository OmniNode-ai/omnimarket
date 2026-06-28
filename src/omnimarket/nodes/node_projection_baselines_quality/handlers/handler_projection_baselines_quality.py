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

from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    ModelBaselinesComputedEvent,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "baselines_quality_snapshots"
CONFLICT_KEY = "snapshot_id"

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
