"""HandlerProjectionBaselines — project baselines-computed events to 4 tables.

Consumes onex.evt.omnibase-infra.baselines-computed.v1 and writes to:
  1. baselines_snapshots (parent row)
  2. baselines_comparisons (per-day treatment-vs-control comparison rows)
  3. baselines_trend (per-cohort per-day trend rows)
  4. baselines_breakdown (per-pattern treatment-vs-control breakdown rows)

The write is transactional in intent: snapshot + all children are upserted via
the same DatabaseAdapter call sequence, keyed for replay safety.

OMN-14513: this handler previously declared its own slim local models
(``ModelBaselinesComputedEvent`` / ``ModelBaselinesComparison`` / ...) with
``extra="ignore"`` and a required-no-default ``pattern_id`` the producer never
sends. The producer's own model is ``extra="forbid"`` with almost no
field-name overlap with the local copy, so every real event raised a
``ValidationError`` (or, for the top-level snapshot write, a DB error from
writing columns -- ``patterns_compared``/``patterns_recommended`` -- that do
not exist on ``baselines_snapshots``) that was silently swallowed by the
runtime's catch-all dispatch boundary. Confirmed live on .201 stability-test:
the consumer group had committed offset 2 with zero lag (both real events
consumed) yet all four target tables held zero rows.

Fix: consume the producer's CANONICAL event model directly rather than a
local copy. Market is the top layer (compat < core < spi < infra < market),
so importing omnibase_infra here is legal, not a layering inversion.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_infra.services.observability.baselines.models.model_baselines_breakdown_row import (
    ModelBaselinesBreakdownRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
    ModelBaselinesComparisonRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_trend_row import (
    ModelBaselinesTrendRow,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE_SNAPSHOTS = "baselines_snapshots"
TABLE_COMPARISONS = "baselines_comparisons"
TABLE_TREND = "baselines_trend"
TABLE_BREAKDOWN = "baselines_breakdown"
CONFLICT_KEY = "snapshot_id"


def _strip_transport_keys(data: dict[str, object]) -> dict[str, object]:
    """Drop runtime/transport-injected ``_``-prefixed keys from a decoded payload.

    The canonical producer model is ``extra="forbid"``, so this is load-bearing:
    the decode path attaches transport metadata alongside the payload fields
    (``unwrap_envelope`` adds ``_envelope``/``_event_type``/``_correlation_id``;
    the RuntimeLocal shim adds ``_db``/``_topic``/etc). Without stripping them,
    validation would raise on every single message.
    """
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    tables_written: list[str] = Field(default_factory=list)


class HandlerProjectionBaselines:
    """Project baselines-computed events into 4 baselines tables."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelBaselinesSnapshotEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        payload = _strip_transport_keys(input_data)
        # A ValidationError from a malformed payload is NOT caught here — it
        # propagates to the wiring layer (_make_projection_dispatch_callback),
        # which routes it to the contract-declared event_bus.dlq_topics.
        # dlq-path-not-required: propagates to the wiring layer's dlq_topics route (OMN-13548)
        event = ModelBaselinesSnapshotEvent(**payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelBaselinesSnapshotEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a baselines snapshot with all child rows."""
        now = datetime.now(tz=UTC).isoformat()
        rows_total = 0
        tables_written: list[str] = []
        snapshot_id = str(event.snapshot_id)

        # 1. Snapshot parent row
        snapshot_row: dict[str, object] = {
            "snapshot_id": snapshot_id,
            "contract_version": event.contract_version,
            "computed_at_utc": event.computed_at_utc.isoformat(),
            "window_start_utc": event.window_start_utc.isoformat()
            if event.window_start_utc
            else None,
            "window_end_utc": event.window_end_utc.isoformat()
            if event.window_end_utc
            else None,
            "projected_at": now,
        }
        if db.upsert(TABLE_SNAPSHOTS, CONFLICT_KEY, snapshot_row):
            rows_total += 1
            tables_written.append(TABLE_SNAPSHOTS)

        # 2. Comparison child rows (daily treatment-vs-control)
        for comp in event.comparisons:
            comp_row = _comparison_row(snapshot_id, comp)
            if db.upsert(TABLE_COMPARISONS, "id", comp_row):
                rows_total += 1
        if event.comparisons:
            tables_written.append(TABLE_COMPARISONS)

        # 3. Trend child rows (per-cohort per-day)
        for tr in event.trend:
            trend_row = _trend_row(snapshot_id, tr)
            if db.upsert(TABLE_TREND, "id", trend_row):
                rows_total += 1
        if event.trend:
            tables_written.append(TABLE_TREND)

        # 4. Breakdown child rows (per-pattern treatment-vs-control)
        for bd in event.breakdown:
            breakdown_row = _breakdown_row(snapshot_id, bd)
            if db.upsert(TABLE_BREAKDOWN, "id", breakdown_row):
                rows_total += 1
        if event.breakdown:
            tables_written.append(TABLE_BREAKDOWN)

        return ModelProjectionResult(
            rows_upserted=rows_total,
            tables_written=tables_written,
        )


def _comparison_row(
    snapshot_id: str, comp: ModelBaselinesComparisonRow
) -> dict[str, object]:
    return {
        "id": str(comp.id),
        "snapshot_id": snapshot_id,
        "comparison_date": comp.comparison_date.isoformat(),
        "period_label": comp.period_label,
        "treatment_sessions": comp.treatment_sessions,
        "treatment_success_rate": comp.treatment_success_rate,
        "treatment_avg_latency_ms": comp.treatment_avg_latency_ms,
        "treatment_avg_cost_tokens": comp.treatment_avg_cost_tokens,
        "treatment_total_tokens": comp.treatment_total_tokens,
        "control_sessions": comp.control_sessions,
        "control_success_rate": comp.control_success_rate,
        "control_avg_latency_ms": comp.control_avg_latency_ms,
        "control_avg_cost_tokens": comp.control_avg_cost_tokens,
        "control_total_tokens": comp.control_total_tokens,
        "roi_pct": comp.roi_pct,
        "latency_improvement_pct": comp.latency_improvement_pct,
        "cost_improvement_pct": comp.cost_improvement_pct,
        "sample_size": comp.sample_size,
        "computed_at": comp.computed_at.isoformat(),
        "created_at": comp.created_at.isoformat(),
        "updated_at": comp.updated_at.isoformat(),
    }


def _trend_row(snapshot_id: str, tr: ModelBaselinesTrendRow) -> dict[str, object]:
    return {
        "id": str(tr.id),
        "snapshot_id": snapshot_id,
        "trend_date": tr.trend_date.isoformat(),
        "cohort": tr.cohort,
        "session_count": tr.session_count,
        "success_rate": tr.success_rate,
        "avg_latency_ms": tr.avg_latency_ms,
        "avg_cost_tokens": tr.avg_cost_tokens,
        "roi_pct": tr.roi_pct,
        "computed_at": tr.computed_at.isoformat(),
        "created_at": tr.created_at.isoformat(),
    }


def _breakdown_row(
    snapshot_id: str, bd: ModelBaselinesBreakdownRow
) -> dict[str, object]:
    return {
        "id": str(bd.id),
        "snapshot_id": snapshot_id,
        "pattern_id": str(bd.pattern_id),
        "pattern_label": bd.pattern_label,
        "treatment_success_rate": bd.treatment_success_rate,
        "control_success_rate": bd.control_success_rate,
        "roi_pct": bd.roi_pct,
        "sample_count": bd.sample_count,
        "treatment_count": bd.treatment_count,
        "control_count": bd.control_count,
        "confidence": bd.confidence,
        "computed_at": bd.computed_at.isoformat(),
        "created_at": bd.created_at.isoformat(),
        "updated_at": bd.updated_at.isoformat(),
    }


__all__: list[str] = [
    "HandlerProjectionBaselines",
    "ModelProjectionResult",
]
