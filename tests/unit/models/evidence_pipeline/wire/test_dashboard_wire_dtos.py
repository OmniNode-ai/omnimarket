# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

import pytest
from pydantic import ValidationError

from omnimarket.models.evidence_pipeline.wire.model_correlation_trace import (
    ModelCorrelationTrace,
    ModelCorrelationTraceEvent,
)
from omnimarket.models.evidence_pipeline.wire.model_correlation_trace_projection import (
    ModelCorrelationTraceProjection,
)
from omnimarket.models.evidence_pipeline.wire.model_dashboard_event import (
    ModelDashboardEvent,
)
from omnimarket.models.evidence_pipeline.wire.model_dashboard_projection_event import (
    ModelDashboardProjectionEvent,
)
from omnimarket.models.evidence_pipeline.wire.model_evidence_dashboard_projection import (
    ModelEvidenceDashboardProjection,
    ModelEvidenceDashboardStageProjection,
)
from omnimarket.models.evidence_pipeline.wire.model_readiness_aggregate_projection import (
    ModelReadinessAggregateProjection,
)
from omnimarket.models.evidence_pipeline.wire.topics import (
    EVIDENCE_COLLECTED_EVT_V1,
    EVIDENCE_PIPELINE_START_CMD_V1,
    EVIDENCE_VALIDATED_EVT_V1,
)


def _trace_event(
    *,
    event_id: str = "evt-001",
    sequence: int = 1,
    stage: str = "TRIGGERED",
) -> ModelCorrelationTraceEvent:
    return ModelCorrelationTraceEvent(
        event_id=event_id,
        projection_cursor=f"cursor-{sequence}",
        ingest_sequence=sequence,
        topic=EVIDENCE_PIPELINE_START_CMD_V1,
        stage=stage,
        status="IN_FLIGHT",
        timestamp="2026-05-21T20:00:00Z",
        evidence_lifecycle_state="PROVISIONAL",
        payload_summary={"ticket_id": "OMN-11469"},
    )


@pytest.mark.unit
def test_dashboard_projection_event_carries_normalization_contract() -> None:
    event = ModelDashboardProjectionEvent(
        event_id="dashboard-event-001",
        causation_id="source-event-001",
        source_event_type="EvidenceCollected",
        normalized_stage="COLLECTED",
        normalized_status="IN_FLIGHT",
        severity="INFO",
        lifecycle_state="PROVISIONAL",
        source_event_hash="sha256:source",
        projection_cursor="projection:1",
        ingest_sequence=1,
        correlation_id="corr-001",
        ticket_id="OMN-11469",
        topic=EVIDENCE_COLLECTED_EVT_V1,
        observed_at="2026-05-21T20:01:00Z",
    )

    restored = ModelDashboardProjectionEvent.model_validate_json(
        event.model_dump_json()
    )
    assert restored == event
    assert restored.projection_cursor == "projection:1"
    assert restored.ingest_sequence == 1


@pytest.mark.unit
def test_dashboard_event_is_projection_write_shape() -> None:
    event = ModelDashboardEvent(
        event_id="dashboard-event-002",
        causation_id="source-event-002",
        source_event_hash="sha256:source",
        projection_cursor="projection:2",
        ingest_sequence=2,
        correlation_id="corr-001",
        ticket_id="OMN-11469",
        topic=EVIDENCE_VALIDATED_EVT_V1,
        stage="VALIDATED",
        timestamp="2026-05-21T20:03:00Z",
        payload_summary={"result": "passed"},
        evidence_lifecycle_state="VALIDATED",
    )

    assert event.payload_summary["result"] == "passed"
    assert event.stage == "VALIDATED"

    with pytest.raises(TypeError):
        event.payload_summary["result"] = "failed"

    restored = ModelDashboardEvent.model_validate_json(event.model_dump_json())
    assert restored == event


@pytest.mark.unit
def test_correlation_trace_requires_ingest_sequence_ordering() -> None:
    trace = ModelCorrelationTrace(
        correlation_id="corr-001",
        events=(
            _trace_event(event_id="evt-001", sequence=1),
            _trace_event(event_id="evt-002", sequence=2, stage="COLLECTED"),
        ),
        total_latency_ms=1200,
    )

    assert [event.ingest_sequence for event in trace.events] == [1, 2]
    assert trace.ordered_by == "ingest_sequence"

    with pytest.raises(ValidationError):
        ModelCorrelationTrace(
            correlation_id="corr-001",
            events=(
                _trace_event(event_id="evt-002", sequence=2, stage="COLLECTED"),
                _trace_event(event_id="evt-001", sequence=1),
            ),
        )


@pytest.mark.unit
def test_evidence_dashboard_projection_carries_reducer_metadata() -> None:
    projection = ModelEvidenceDashboardProjection(
        projection_cursor="projection:5",
        last_event_id="event-005",
        last_ingest_sequence=5,
        freshness_state="CURRENT",
        observed_at="2026-05-21T20:05:00Z",
        version="1.0.0",
        stages=(
            ModelEvidenceDashboardStageProjection(
                stage="VALIDATED",
                status="PASSED",
                event_count=3,
                stale_event_count=0,
                freshness_state="CURRENT",
                last_projection_update_at="2026-05-21T20:05:00Z",
                correlation_ids=("corr-001",),
            ),
        ),
    )

    assert projection.last_event_id == "event-005"
    assert projection.stages[0].event_count == 3


@pytest.mark.unit
def test_projection_degraded_state_requires_reason() -> None:
    with pytest.raises(ValidationError):
        ModelEvidenceDashboardProjection(
            projection_cursor="projection:5",
            last_event_id="event-005",
            last_ingest_sequence=5,
            freshness_state="DEGRADED",
            observed_at="2026-05-21T20:05:00Z",
        )


@pytest.mark.unit
def test_correlation_trace_projection_rejects_invalid_order_and_cursor_gap() -> None:
    with pytest.raises(ValidationError):
        ModelCorrelationTraceProjection(
            correlation_id="corr-001",
            projection_cursor="projection:2",
            source_event_ids=("evt-002", "evt-001"),
            last_event_id="evt-002",
            last_ingest_sequence=2,
            freshness_state="CURRENT",
            observed_at="2026-05-21T20:02:00Z",
            events=(
                _trace_event(event_id="evt-002", sequence=2, stage="COLLECTED"),
                _trace_event(event_id="evt-001", sequence=1),
            ),
            missing_event_classifications={"EXTRACTED": "DELAYED"},
        )

    with pytest.raises(ValidationError):
        ModelCorrelationTraceProjection(
            correlation_id="corr-001",
            projection_cursor="projection:1",
            source_event_ids=("evt-002",),
            last_event_id="evt-002",
            last_ingest_sequence=1,
            freshness_state="CURRENT",
            observed_at="2026-05-21T20:02:00Z",
            events=(_trace_event(event_id="evt-002", sequence=2, stage="COLLECTED"),),
        )


@pytest.mark.unit
def test_readiness_aggregate_projection_keeps_readiness_separate_from_pipeline_state() -> (
    None
):
    projection = ModelReadinessAggregateProjection(
        deployment_id="deployment-001",
        projection_cursor="projection:9",
        last_event_id="event-009",
        last_ingest_sequence=9,
        freshness_state="STALE",
        observed_at="2026-05-21T20:09:00Z",
        readiness_state="BLOCKED",
        evidence_pipeline_state="PASSED",
        gap_breakdown={"MISSING": 2, "STALE": 1},
        blocking_reason_codes=("RECEIPT_MISSING",),
        correlation_ids=("corr-001",),
        ticket_ids=("OMN-11469",),
    )

    assert projection.readiness_state == "BLOCKED"
    assert projection.evidence_pipeline_state == "PASSED"
    assert projection.gap_breakdown["MISSING"] == 2

    with pytest.raises(TypeError):
        projection.gap_breakdown["MISSING"] = 3

    restored = ModelReadinessAggregateProjection.model_validate_json(
        projection.model_dump_json()
    )
    assert restored == projection


@pytest.mark.unit
def test_readiness_aggregate_projection_rejects_negative_gap_counts() -> None:
    with pytest.raises(ValidationError):
        ModelReadinessAggregateProjection(
            deployment_id="deployment-001",
            projection_cursor="projection:9",
            last_event_id="event-009",
            last_ingest_sequence=9,
            freshness_state="CURRENT",
            observed_at="2026-05-21T20:09:00Z",
            readiness_state="BLOCKED",
            evidence_pipeline_state="PASSED",
            gap_breakdown={"MISSING": -1},
        )


@pytest.mark.unit
def test_dashboard_literals_and_frozen_models_are_enforced() -> None:
    event = ModelDashboardProjectionEvent(
        event_id="dashboard-event-001",
        source_event_type="EvidenceCollected",
        normalized_stage="COLLECTED",
        normalized_status="IN_FLIGHT",
        severity="INFO",
        lifecycle_state="PROVISIONAL",
        source_event_hash="sha256:source",
        projection_cursor="projection:1",
        ingest_sequence=1,
        correlation_id="corr-001",
        topic=EVIDENCE_COLLECTED_EVT_V1,
        observed_at="2026-05-21T20:01:00Z",
    )

    with pytest.raises(ValidationError):
        event.topic = "changed"

    with pytest.raises(ValidationError):
        ModelDashboardProjectionEvent(
            event_id="dashboard-event-001",
            source_event_type="EvidenceCollected",
            normalized_stage="COLLECTED",
            normalized_status="IN_FLIGHT",
            severity="CRITICAL",
            lifecycle_state="PROVISIONAL",
            source_event_hash="sha256:source",
            projection_cursor="projection:1",
            ingest_sequence=1,
            correlation_id="corr-001",
            topic=EVIDENCE_COLLECTED_EVT_V1,
            observed_at="2026-05-21T20:01:00Z",
        )


@pytest.mark.unit
def test_dashboard_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelDashboardEvent(
            event_id="dashboard-event-002",
            source_event_hash="sha256:source",
            projection_cursor="projection:2",
            ingest_sequence=2,
            correlation_id="corr-001",
            topic=EVIDENCE_VALIDATED_EVT_V1,
            stage="VALIDATED",
            timestamp="2026-05-21T20:03:00Z",
            evidence_lifecycle_state="VALIDATED",
            unexpected="nope",
        )


@pytest.mark.unit
def test_projection_models_round_trip() -> None:
    projection = ModelCorrelationTraceProjection(
        correlation_id="corr-001",
        projection_cursor="projection:2",
        source_event_ids=("evt-001", "evt-002"),
        last_event_id="evt-002",
        last_ingest_sequence=2,
        freshness_state="CURRENT",
        observed_at="2026-05-21T20:02:00Z",
        events=(
            _trace_event(event_id="evt-001", sequence=1),
            _trace_event(event_id="evt-002", sequence=2, stage="COLLECTED"),
        ),
        missing_event_classifications={"EXTRACTED": "DELAYED"},
    )

    restored = ModelCorrelationTraceProjection.model_validate_json(
        projection.model_dump_json()
    )
    assert restored == projection
