# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionBaselinesQuality — project baseline quality summary to DB.

Consumes onex.evt.omnibase-infra.baselines-computed.v1 and aggregates the
per-pattern breakdown + comparison rows into a single
baselines_quality_snapshots row per snapshot_id. The table backs the
projection API topic onex.snapshot.projection.baselines.quality.v1 consumed
by the omnidash quality-baseline-panel widget.

OMN-14630: this handler previously imported ``ModelBaselinesComputedEvent``
from ``node_projection_baselines`` — a fictional local model
(``patterns_compared``/``patterns_recommended``/per-comparison string
``confidence`` tiers) that shares almost no field names with the real
producer contract (``omnibase_infra...ModelBaselinesSnapshotEvent``).
OMN-14513 fixed ``node_projection_baselines``'s own consumer path; to avoid
a compile break here it temporarily gave this handler an unchanged LOCAL
COPY of the same fictional models (see OMN-14513 PR history) — a deliberate
no-op, not a fix. Confirmed live on .201 stability-test (2026-07-14): this
node's own consumer group (``projection_baselines_quality``) had committed
offset 2 with zero lag (both real events consumed) yet
``baselines_quality_snapshots`` held zero rows — the same swallowed-crash
failure mode OMN-14513 found and fixed for the sibling node.

Fix: consume the producer's CANONICAL event model directly. Market is the
top layer (compat < core < spi < infra < market), so importing
omnibase_infra here is legal, not a layering inversion.

Aggregation semantics (redesigned onto the real producer schema — the old
per-comparison string ``confidence`` tier and the ``patterns_recommended``/
``recommend_rate`` "recommendation" concept have NO analog on the real
event and are redefined against real fields, not remapped 1:1):

  patterns_compared     — len(event.breakdown): one row per distinct
                           pattern (``selected_agent``) in this snapshot.
  patterns_significant  — count of breakdown rows where
                           ``confidence is not None`` (the real event's
                           ``confidence`` is a proxy that is only non-null
                           when ``sample_count >= 20`` — i.e. the pattern
                           has enough data to be statistically meaningful;
                           replaces the fictional "recommended" concept
                           with a genuine sufficiency signal).
  high_confidence_count    — breakdown rows with confidence >= 0.8.
  medium_confidence_count  — breakdown rows with 0.5 <= confidence < 0.8.
  low_confidence_count     — breakdown rows with confidence < 0.5 OR
                              confidence is None (insufficient sample size
                              is treated as low confidence, not excluded).
  quality_score          — mean(comparison.treatment_success_rate for
                            comparisons where non-null); 0.0 when no
                            comparisons. The real producer's own
                            "how well is the treatment cohort doing"
                            signal, replacing the old fictional
                            confidence-tier-weighted score.
  significant_rate        — patterns_significant / max(1, patterns_compared).
                            Replaces the fictional ``recommend_rate`` with
                            the fraction of patterns backed by a
                            statistically sufficient sample.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "baselines_quality_snapshots"
CONFLICT_KEY = "snapshot_id"

_HIGH_THRESHOLD = 0.8
_MEDIUM_THRESHOLD = 0.5


def _strip_transport_keys(data: dict[str, object]) -> dict[str, object]:
    """Drop runtime/transport-injected ``_``-prefixed keys from a decoded payload.

    The canonical producer model is ``extra="forbid"``, so this is
    load-bearing: the decode path attaches transport metadata alongside the
    payload fields (``unwrap_envelope`` adds ``_envelope``/``_event_type``/
    ``_correlation_id``; the RuntimeLocal shim adds ``_db``/``_topic``/etc).
    Without stripping them, validation would raise on every single message.
    """
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


class ModelBaselinesQualityProjectionResult(BaseModel):
    """Result returned by HandlerProjectionBaselinesQuality.project()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionBaselinesQuality:
    """Project baselines-computed events into the baselines_quality_snapshots table."""

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
    ) -> ModelBaselinesQualityProjectionResult:
        """Aggregate event into one baselines_quality_snapshots row and UPSERT."""
        patterns_compared = len(event.breakdown)

        high_confidence_count = 0
        medium_confidence_count = 0
        low_confidence_count = 0
        patterns_significant = 0

        for bd in event.breakdown:
            if bd.confidence is None:
                low_confidence_count += 1
                continue
            patterns_significant += 1
            if bd.confidence >= _HIGH_THRESHOLD:
                high_confidence_count += 1
            elif bd.confidence >= _MEDIUM_THRESHOLD:
                medium_confidence_count += 1
            else:
                low_confidence_count += 1

        success_rates = [
            c.treatment_success_rate
            for c in event.comparisons
            if c.treatment_success_rate is not None
        ]
        quality_score = (
            sum(success_rates) / len(success_rates) if success_rates else 0.0
        )

        significant_rate = (
            patterns_significant / patterns_compared if patterns_compared > 0 else 0.0
        )

        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "snapshot_id": str(event.snapshot_id),
            "captured_at": event.computed_at_utc.isoformat(),
            "patterns_compared": patterns_compared,
            "patterns_significant": patterns_significant,
            "high_confidence_count": high_confidence_count,
            "medium_confidence_count": medium_confidence_count,
            "low_confidence_count": low_confidence_count,
            "quality_score": quality_score,
            "significant_rate": significant_rate,
            "projected_at": now,
        }
        db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelBaselinesQualityProjectionResult(rows_upserted=1)


__all__: list[str] = [
    "HandlerProjectionBaselinesQuality",
    "ModelBaselinesQualityProjectionResult",
]
