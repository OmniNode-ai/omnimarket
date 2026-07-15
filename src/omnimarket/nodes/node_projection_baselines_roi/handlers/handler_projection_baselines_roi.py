# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionBaselinesRoi — project baseline ROI summary to DB.

Consumes onex.evt.omnibase-infra.baselines-computed.v1 and aggregates the
daily treatment-vs-control comparison rows into a single
baselines_roi_snapshots row per snapshot_id. The table backs the projection
API topic onex.snapshot.projection.baselines.roi.v1 consumed by the
omnidash BaselinesROICard widget.

OMN-14630: this handler previously imported ``ModelBaselinesComputedEvent``
from ``node_projection_baselines`` — a fictional local model
(``pattern_id``/``token_delta``/``time_delta_s``/``confidence``/
``recommendations``/``retry_counts``) that shares almost no field names with
the real producer contract
(``omnibase_infra...ModelBaselinesSnapshotEvent``). OMN-14513 fixed
``node_projection_baselines``'s own consumer path; to avoid a compile break
here it temporarily gave this handler an unchanged LOCAL COPY of the same
fictional models (see OMN-14513 PR history) — a deliberate no-op, not a fix.
Confirmed live on .201 stability-test (2026-07-14): this node's own consumer
group (``projection_baselines_roi``) had committed offset 2 with zero lag
(both real events consumed) yet ``baselines_roi_snapshots`` held zero rows
— the same swallowed-crash failure mode OMN-14513 found and fixed for the
sibling node.

Fix: consume the producer's CANONICAL event model directly. Market is the
top layer (compat < core < spi < infra < market), so importing
omnibase_infra here is legal, not a layering inversion.

Aggregation semantics (redesigned onto the real producer schema — the old
``token_delta``/``time_delta_ms``/``retry_delta``/``recommendations``/
``confidence`` fields have NO analog on ``ModelBaselinesComparisonRow`` and
are dropped, not remapped):

  token_delta       — sum(control_total_tokens) - sum(treatment_total_tokens)
                       across comparisons. Positive = treatment cohort used
                       fewer tokens than control (a real token-savings
                       signal from producer-native fields, replacing the
                       old fictional per-pattern ``token_delta``).
  roi_pct_avg       — mean of non-null comparison.roi_pct.
  latency_improvement_pct_avg — mean of non-null comparison.latency_improvement_pct.
  cost_improvement_pct_avg    — mean of non-null comparison.cost_improvement_pct.
  sample_size       — sum of comparison.sample_size across comparisons
                       (replaces the old fictional ``confidence`` tier
                       average — the real event carries no confidence-tier
                       concept; total sample size is the closest honest
                       "how much data backs this" signal).
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "baselines_roi_snapshots"
CONFLICT_KEY = "snapshot_id"


def _strip_transport_keys(data: dict[str, object]) -> dict[str, object]:
    """Drop runtime/transport-injected ``_``-prefixed keys from a decoded payload.

    The canonical producer model is ``extra="forbid"``, so this is
    load-bearing: the decode path attaches transport metadata alongside the
    payload fields (``unwrap_envelope`` adds ``_envelope``/``_event_type``/
    ``_correlation_id``; the RuntimeLocal shim adds ``_db``/``_topic``/etc).
    Without stripping them, validation would raise on every single message.
    """
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class ModelBaselinesRoiProjectionResult(BaseModel):
    """Result returned by HandlerProjectionBaselinesRoi.project()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionBaselinesRoi:
    """Project baselines-computed events into the baselines_roi_snapshots table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Pops ``_db`` from input_data, constructs a ModelBaselinesSnapshotEvent
        from the stripped payload, delegates to project(), and returns the
        result as a plain dict.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")

        payload = _strip_transport_keys(payload)
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
    ) -> ModelBaselinesRoiProjectionResult:
        """Aggregate event into one baselines_roi_snapshots row and UPSERT."""
        comparisons = event.comparisons

        treatment_total_tokens = sum(c.treatment_total_tokens for c in comparisons)
        control_total_tokens = sum(c.control_total_tokens for c in comparisons)
        token_delta = control_total_tokens - treatment_total_tokens

        roi_pct_avg = _mean([c.roi_pct for c in comparisons if c.roi_pct is not None])
        latency_improvement_pct_avg = _mean(
            [
                c.latency_improvement_pct
                for c in comparisons
                if c.latency_improvement_pct is not None
            ]
        )
        cost_improvement_pct_avg = _mean(
            [
                c.cost_improvement_pct
                for c in comparisons
                if c.cost_improvement_pct is not None
            ]
        )
        sample_size = sum(c.sample_size for c in comparisons)

        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "snapshot_id": str(event.snapshot_id),
            "captured_at": event.computed_at_utc.isoformat(),
            "token_delta": token_delta,
            "roi_pct_avg": roi_pct_avg,
            "latency_improvement_pct_avg": latency_improvement_pct_avg,
            "cost_improvement_pct_avg": cost_improvement_pct_avg,
            "sample_size": sample_size,
            "projected_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelBaselinesRoiProjectionResult(rows_upserted=1)


__all__: list[str] = [
    "HandlerProjectionBaselinesRoi",
    "ModelBaselinesRoiProjectionResult",
]
