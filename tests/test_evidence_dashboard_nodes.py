from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_evidence_dashboard_effect.handlers.handler_evidence_dashboard_effect import (
    SOURCE_TOPICS,
    HandlerEvidenceDashboardEffect,
)
from omnimarket.nodes.node_evidence_dashboard_reducer.handlers.handler_evidence_dashboard_reducer import (
    DASHBOARD_TABLE,
    READINESS_TABLE,
    TRACE_TABLE,
    HandlerEvidenceDashboardReducer,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

EXPECTED_SOURCE_TOPICS = (
    "onex.cmd.omnimarket.evidence-pipeline-start.v1",
    "onex.evt.omnimarket.evidence-collected.v1",
    "onex.evt.omnimarket.evidence-extracted.v1",
    "onex.evt.omnimarket.evidence-validated.v1",
    "onex.evt.omnimarket.occ-pr-created.v1",
    "onex.evt.omnimarket.evidence-pipeline-completed.v1",
    "onex.cmd.omnimarket.readiness-gate-start.v1",
    "onex.evt.omnimarket.readiness-gate-completed.v1",
    "onex.evt.omnimarket.readiness-gate-blocked.v1",
)


def _contract(node_name: str) -> dict[str, object]:
    with (NODES_ROOT / node_name / "contract.yaml").open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def test_effect_contract_subscribes_to_exact_dashboard_source_topics() -> None:
    contract = _contract("node_evidence_dashboard_effect")

    assert tuple(contract["event_bus"]["subscribe_topics"]) == EXPECTED_SOURCE_TOPICS
    assert SOURCE_TOPICS == EXPECTED_SOURCE_TOPICS
    assert contract["normalization_contract"]["owns_state"] is False


def test_effect_normalizes_to_wave_1_dashboard_vocabulary() -> None:
    event = HandlerEvidenceDashboardEffect().normalize(
        {
            "_topic": "onex.evt.omnimarket.readiness-gate-blocked.v1",
            "_partition": 0,
            "_offset": 12,
            "event_id": "evt-blocked",
            "correlation_id": "corr-1",
            "ticket_id": "OMN-11468",
            "observed_at": "2026-05-21T23:00:00Z",
        }
    )

    assert event.topic == "onex.evt.omnimarket.readiness-gate-blocked.v1"
    assert event.normalized_stage == "READINESS_GATE_BLOCKED"
    assert event.normalized_status == "BLOCKED"
    assert event.severity == "BLOCKING"
    assert event.lifecycle_state == "REJECTED"
    assert event.projection_cursor == (
        "onex.evt.omnimarket.readiness-gate-blocked.v1:0:12"
    )
    assert event.source_event_hash.startswith("sha256:")


def test_effect_handle_payload_matches_canonical_contract_fields() -> None:
    payload = HandlerEvidenceDashboardEffect().handle(
        {
            "_topic": "onex.evt.omnimarket.evidence-collected.v1",
            "event_id": "evt-collected",
            "ingest_sequence": 10,
            "correlation_id": "corr-1",
        }
    )

    assert payload["normalized_status"] == "PASSED"
    assert payload["topic"] == "onex.evt.omnimarket.evidence-collected.v1"
    assert "status" not in payload
    assert "source_topic" not in payload


def test_reducer_contract_exposes_versioned_projection_topics() -> None:
    contract = _contract("node_evidence_dashboard_reducer")
    exposures = contract["projection_api"]["exposures"]
    topics = {exposure["topic"] for exposure in exposures}

    assert topics == {
        "onex.snapshot.projection.evidence_pipeline.stages.v1",
        "onex.snapshot.projection.evidence_pipeline.correlations.v1",
        "onex.snapshot.projection.evidence_pipeline.readiness.v1",
        "onex.snapshot.projection.evidence_pipeline.live_events.v1",
    }
    for exposure in exposures:
        assert exposure["cursor_column"] == "projection_cursor"
        assert exposure["last_event_id_column"] == "last_event_id"
        assert exposure["last_ingest_sequence_column"] == "last_ingest_sequence"
        assert exposure["freshness_state_column"] == "freshness_state"
        assert exposure["observed_at_column"] == "observed_at"


def test_reducer_materializes_three_projection_tables_idempotently() -> None:
    event = HandlerEvidenceDashboardEffect().normalize(
        {
            "_topic": "onex.evt.omnimarket.readiness-gate-blocked.v1",
            "event_id": "evt-blocked",
            "ingest_sequence": 12,
            "correlation_id": "corr-1",
            "ticket_id": "OMN-11468",
            "observed_at": "2026-05-21T23:00:00Z",
        }
    )
    db = InmemoryDatabaseAdapter()
    result = HandlerEvidenceDashboardReducer().project(event, db)
    replay = HandlerEvidenceDashboardReducer().project(event, db)

    assert result.rows_upserted == 3
    assert replay.rows_upserted == 3
    assert len(db.tables[DASHBOARD_TABLE]) == 1
    assert len(db.tables[TRACE_TABLE]) == 1
    assert len(db.tables[READINESS_TABLE]) == 1
    assert db.tables[DASHBOARD_TABLE][0]["freshness_state"] == "DEGRADED"
    assert db.tables[DASHBOARD_TABLE][0]["last_ingest_sequence"] == 12
    assert db.tables[READINESS_TABLE][0]["readiness_state"] == "BLOCKED"
    assert db.tables[READINESS_TABLE][0]["total_events"] == 1
