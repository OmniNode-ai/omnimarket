"""Cross-boundary seam regression: producer wire dump -> BOTH consumer paths.

OMN-14513: node_projection_baselines declared local Pydantic models that
shared almost no field names with the real producer contract
(``omnibase_infra...ModelBaselinesSnapshotEvent``) — a required-no-default
``pattern_id`` the producer never sends, entire concepts (``trend``/
``breakdown``) with no consumer equivalent, and a snapshot write that bound
columns (``patterns_compared``/``patterns_recommended``) that do not exist on
``baselines_snapshots``. Confirmed live on .201 stability-test: the consumer
group had committed offset 2 with zero lag (both real events on the topic
had been consumed) yet all four target tables held zero rows.

This test is deliberately NOT a unit suite against a hand-rolled fixture. It:

  1. constructs the PRODUCER's canonical model (omnibase_infra) with rich A/B
     treatment/control values,
  2. wraps + serializes it exactly as the producer does (``ModelEventEnvelope``
     -> JSON bytes),
  3. decodes it through the consumer's REAL decode seam
     (``omnimarket.projection.envelope.unwrap_envelope``, which injects the
     ``_envelope`` transport key), and
  4. drives that dict through BOTH real consumer paths, asserting the four
     target tables receive real (non-default) data.

Both consumer paths are covered because there are two of them and fixing
only one leaves the bug alive on whichever deployment topology wires the
other (``docker-compose.projection.yml`` still declares
``BaselinesProjectionRunner`` as a standalone deployable):

  * ``HandlerProjectionBaselines``  (the RuntimeLocal handler shim; the path
    actually wired live on .201 via this node's entry point)
  * ``BaselinesProjectionRunner``   (the standalone Kafka -> Postgres runner)

RED-vs-exists-but-wrong: against the pre-fix local models, path 1 raises
``ValidationError`` (missing required ``pattern_id`` is never true here
because the field does not exist on the real row at all — the old model
would raise on construction from the real payload) and path 2 silently wrote
rows keyed on a blank ``pattern_id`` (skipped) or degraded to all-default
values. Green requires the canonical model to carry the real fields through
end to end.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
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
from omnibase_infra.services.observability.baselines.models.model_baselines_trend_row import (
    ModelBaselinesTrendRow,
)

from omnimarket.nodes.node_projection_baselines.handlers.handler_baselines import (
    BaselinesProjectionRunner,
)
from omnimarket.nodes.node_projection_baselines.handlers.handler_projection_baselines import (
    HandlerProjectionBaselines,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta

TOPIC = "onex.evt.omnibase-infra.baselines-computed.v1"

SNAPSHOT_ID = UUID("11111111-2222-3333-4444-555555555555")
COMPARISON_ID = UUID("aaaaaaaa-1111-1111-1111-111111111111")
TREND_ID = UUID("bbbbbbbb-2222-2222-2222-222222222222")
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
        trend=[
            ModelBaselinesTrendRow(
                id=TREND_ID,
                trend_date=date(2026, 7, 14),
                cohort="treatment",
                session_count=120,
                success_rate=0.94,
                avg_latency_ms=850.0,
                avg_cost_tokens=1200.0,
                roi_pct=32.4,
                computed_at=NOW,
                created_at=NOW,
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


# --------------------------------------------------------------------------
# Path 1: HandlerProjectionBaselines (RuntimeLocal handler shim -- the path
# actually wired live on .201 via node_projection_baselines's entry point)
# --------------------------------------------------------------------------


class TestHandlerPathSeam:
    def test_real_snapshot_populates_all_four_tables(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_snapshot())

        result_raw = HandlerProjectionBaselines().handle({**payload, "_db": db})

        assert result_raw["rows_upserted"] == 4
        assert set(result_raw["tables_written"]) == {
            "baselines_snapshots",
            "baselines_comparisons",
            "baselines_trend",
            "baselines_breakdown",
        }

        snapshot_row = db.tables["baselines_snapshots"][0]
        assert snapshot_row["snapshot_id"] == str(SNAPSHOT_ID)
        assert snapshot_row["window_start_utc"] is not None

        comp_row = db.tables["baselines_comparisons"][0]
        assert comp_row["id"] == str(COMPARISON_ID)
        assert comp_row["treatment_sessions"] == 120
        assert comp_row["control_sessions"] == 110
        assert comp_row["roi_pct"] == 32.4

        trend_row = db.tables["baselines_trend"][0]
        assert trend_row["id"] == str(TREND_ID)
        assert trend_row["cohort"] == "treatment"
        assert trend_row["session_count"] == 120

        bd_row = db.tables["baselines_breakdown"][0]
        assert bd_row["id"] == str(BREAKDOWN_ID)
        assert bd_row["pattern_id"] == str(PATTERN_ID)
        assert bd_row["treatment_success_rate"] == 0.94

    def test_transport_keys_do_not_leak_or_raise(self) -> None:
        """extra='forbid' on the producer model must tolerate _envelope."""
        db = InmemoryDatabaseAdapter()
        payload = _to_wire(_producer_snapshot())
        assert "_envelope" in payload

        # Must not raise ValidationError on the transport-injected key.
        HandlerProjectionBaselines().handle({**payload, "_db": db})
        assert len(db.tables["baselines_snapshots"]) == 1


# --------------------------------------------------------------------------
# Path 2: BaselinesProjectionRunner (standalone Kafka -> Postgres projector,
# still declared live in docker-compose.projection.yml)
# --------------------------------------------------------------------------


class TestRunnerPathSeam:
    @pytest.mark.asyncio
    async def test_real_snapshot_writes_producer_shaped_sql_binds(self) -> None:
        mock_db = AsyncMock()
        runner = BaselinesProjectionRunner()
        runner._db = mock_db

        payload = _to_wire(_producer_snapshot())
        meta = MessageMeta(partition=0, offset=2, fallback_id=str(uuid4()))

        assert await runner.project_event(TOPIC, payload, meta) is True
        mock_db.execute_in_transaction.assert_called_once()

        queries = mock_db.execute_in_transaction.call_args[0][0]
        # 1 snapshot upsert + (delete+insert) x3 children = 7
        assert len(queries) == 7

        comparison_insert = queries[2]
        assert "baselines_comparisons" in comparison_insert[0]
        comp_binds = comparison_insert[1]
        assert comp_binds[0] == str(COMPARISON_ID)
        assert comp_binds[1] == str(SNAPSHOT_ID)
        # treatment_sessions is bind index 4 in the INSERT column order.
        assert 120 in comp_binds
        assert 110 in comp_binds  # control_sessions
        assert 32.4 in comp_binds  # roi_pct

        trend_insert = queries[4]
        assert "baselines_trend" in trend_insert[0]
        assert "treatment" in trend_insert[1]

        breakdown_insert = queries[6]
        assert "baselines_breakdown" in breakdown_insert[0]
        assert str(PATTERN_ID) in breakdown_insert[1]
