# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam regression: producer wire dump -> roi + quality consumers.

OMN-14630: node_projection_baselines_roi and node_projection_baselines_quality
both subscribe to onex.evt.omnibase-infra.baselines-computed.v1 but, until this
fix, both declared local Pydantic models sharing almost no field names with the
real producer contract (``omnibase_infra...ModelBaselinesSnapshotEvent`` — see
OMN-14513 for the sibling node_projection_baselines fix, which found and fixed
the identical defect class on its own consumer path). Confirmed live on .201
stability-test (2026-07-14): both consumer groups (``projection_baselines_roi``,
``projection_baselines_quality``) had committed offset 2 with zero lag (both
real events on the topic consumed) yet ``baselines_roi_snapshots`` and
``baselines_quality_snapshots`` held zero rows — the events silently crashed
inside the runtime's catch-all dispatch boundary on every delivery.

This test is deliberately NOT a unit suite against a hand-rolled fixture. It:

  1. constructs the PRODUCER's canonical model (omnibase_infra) with rich A/B
     treatment/control values,
  2. wraps + serializes it exactly as the producer does (``ModelEventEnvelope``
     -> JSON bytes),
  3. decodes it through the consumer's REAL decode seam
     (``omnimarket.projection.envelope.unwrap_envelope``, which injects the
     ``_envelope`` transport key), and
  4. drives that dict through BOTH real consumer handlers, asserting each
     target table receives real (non-default) data derived from the producer's
     native fields.

RED-vs-exists-but-wrong: against the pre-fix local models (the OMN-14513
"unchanged copy" placeholder), both handlers raise ``ValidationError`` on
construction from the real payload — the fictional models declare fields
(``pattern_id``, ``token_delta``, ``confidence`` as a string tier) the real
producer never sends, and ``extra="ignore"`` does not help because the
mandatory ``computed_at_utc: str`` field collides with the real event's
``datetime``-typed field of the same name, and ``patterns_compared`` /
``patterns_recommended`` are entirely absent from the real payload. Green
requires the canonical model to carry the real fields through end to end.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.services.observability.baselines.models.model_baselines_breakdown_row import (
    ModelBaselinesBreakdownRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_comparison_row import (
    ModelBaselinesComparisonRow,
)
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)

from omnimarket.nodes.node_projection_baselines_quality.handlers.handler_projection_baselines_quality import (
    HandlerProjectionBaselinesQuality,
)
from omnimarket.nodes.node_projection_baselines_roi.handlers.handler_projection_baselines_roi import (
    HandlerProjectionBaselinesRoi,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

SNAPSHOT_ID = UUID("11111111-2222-3333-4444-555555555555")
COMPARISON_ID = UUID("aaaaaaaa-1111-1111-1111-111111111111")
BREAKDOWN_ID = UUID("cccccccc-3333-3333-3333-333333333333")
PATTERN_ID = UUID("dddddddd-4444-4444-4444-444444444444")
NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _producer_snapshot() -> ModelBaselinesSnapshotEvent:
    """A realistic producer-side snapshot with rich A/B treatment/control data."""
    return ModelBaselinesSnapshotEvent(
        snapshot_id=SNAPSHOT_ID,
        contract_version=1,
        computed_at_utc=NOW,
        window_start_utc=NOW,
        window_end_utc=NOW,
        comparisons=[
            ModelBaselinesComparisonRow(
                id=COMPARISON_ID,
                comparison_date=date(2026, 7, 14),
                period_label="2026-07-14",
                treatment_sessions=120,
                treatment_success_rate=0.94,
                treatment_avg_latency_ms=850.0,
                treatment_avg_cost_tokens=1200.0,
                treatment_total_tokens=144000,
                control_sessions=110,
                control_success_rate=0.71,
                control_avg_latency_ms=1400.0,
                control_avg_cost_tokens=2600.0,
                control_total_tokens=286000,
                roi_pct=32.4,
                latency_improvement_pct=39.3,
                cost_improvement_pct=53.8,
                sample_size=230,
                computed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
        ],
        breakdown=[
            ModelBaselinesBreakdownRow(
                id=BREAKDOWN_ID,
                pattern_id=PATTERN_ID,
                pattern_label="retry-guard",
                treatment_success_rate=0.94,
                control_success_rate=0.71,
                roi_pct=32.4,
                sample_count=230,
                treatment_count=120,
                control_count=110,
                confidence=0.94,
                computed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
        ],
    )


def _to_wire(event: ModelBaselinesSnapshotEvent) -> dict[str, Any]:
    """Serialize via the producer's envelope, then decode via the consumer's seam.

    The producer publishes the payload dict; the runtime wraps it in the
    standard ONEX envelope before it reaches Kafka. The consumer decodes via
    ``unwrap_envelope``, which returns the payload dict PLUS an injected
    ``_envelope`` transport key. Any consumer model that is ``extra="forbid"``
    must tolerate that key, so the test must not hand-roll a clean dict.
    """
    envelope: ModelEventEnvelope[Any] = ModelEventEnvelope(payload=event)
    raw = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
    decoded = unwrap_envelope(raw)
    assert decoded is not None, "producer envelope failed to decode"
    assert "_envelope" in decoded, "decode seam no longer injects _envelope"
    return decoded


class TestRoiHandlerSeam:
    def test_real_snapshot_populates_roi_snapshot_table(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_snapshot())

        result_raw = HandlerProjectionBaselinesRoi().handle({**payload, "_db": db})

        assert result_raw["rows_upserted"] == 1
        assert result_raw["table"] == "baselines_roi_snapshots"

        row = db.tables["baselines_roi_snapshots"][0]
        assert row["snapshot_id"] == str(SNAPSHOT_ID)
        # control_total_tokens (286000) - treatment_total_tokens (144000)
        assert row["token_delta"] == 142000
        assert row["roi_pct_avg"] == 32.4
        assert row["latency_improvement_pct_avg"] == 39.3
        assert row["cost_improvement_pct_avg"] == 53.8
        assert row["sample_size"] == 230

    def test_transport_keys_do_not_leak_or_raise(self) -> None:
        """extra='forbid' on the producer model must tolerate _envelope."""
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_snapshot())
        assert "_envelope" in payload

        # Must not raise ValidationError on the transport-injected key.
        HandlerProjectionBaselinesRoi().handle({**payload, "_db": db})
        assert len(db.tables["baselines_roi_snapshots"]) == 1


class TestQualityHandlerSeam:
    def test_real_snapshot_populates_quality_snapshot_table(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_snapshot())

        result_raw = HandlerProjectionBaselinesQuality().handle({**payload, "_db": db})

        assert result_raw["rows_upserted"] == 1
        assert result_raw["table"] == "baselines_quality_snapshots"

        row = db.tables["baselines_quality_snapshots"][0]
        assert row["snapshot_id"] == str(SNAPSHOT_ID)
        assert row["patterns_compared"] == 1
        assert row["patterns_significant"] == 1  # confidence=0.94 is non-null
        assert row["high_confidence_count"] == 1  # 0.94 >= 0.8
        assert row["quality_score"] == 0.94  # comparison.treatment_success_rate

    def test_transport_keys_do_not_leak_or_raise(self) -> None:
        """extra='forbid' on the producer model must tolerate _envelope."""
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_snapshot())
        assert "_envelope" in payload

        # Must not raise ValidationError on the transport-injected key.
        HandlerProjectionBaselinesQuality().handle({**payload, "_db": db})
        assert len(db.tables["baselines_quality_snapshots"]) == 1
