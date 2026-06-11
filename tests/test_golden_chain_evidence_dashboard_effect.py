# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain for node_evidence_dashboard_effect runtime-envelope delivery.

Proves the evidence-pipeline dashboard chain materializes projections when the
runtime kernel delivers events wrapped in its envelope dict
(``{"payload": <event>, "partition_key": ...}``). Before OMN-12935 the effect
read correlation/event ids off the outer envelope, fabricated a
``projection_cursor``, and lost the real correlation id (propagating a malformed
UUID downstream). The chain here drives the effect normalize -> reducer project
path end to end through the envelope and asserts the real ids survive.

OMN-12936 fixed the shared ``coerce_*`` defect on the typed-model boundary; this
covers the dashboard effect's untyped dict boundary, the secondary defect called
out in OMN-12935.
"""

from __future__ import annotations

from omnimarket.nodes.node_evidence_dashboard_effect.handlers.handler_evidence_dashboard_effect import (
    HandlerEvidenceDashboardEffect,
)
from omnimarket.nodes.node_evidence_dashboard_reducer.handlers.handler_evidence_dashboard_reducer import (
    DASHBOARD_TABLE,
    READINESS_TABLE,
    TRACE_TABLE,
    HandlerEvidenceDashboardReducer,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


def _runtime_delivery(event: dict[str, object]) -> dict[str, object]:
    """Mimic the runtime kernel wrapping a consumed event before dispatch."""
    return {"payload": event, "partition_key": None}


def test_golden_chain_dashboard_effect_materializes_through_runtime_envelope() -> None:
    delivered = _runtime_delivery(
        {
            "_topic": "onex.evt.omnimarket.readiness-gate-blocked.v1",
            "event_id": "evt-blocked",
            "ingest_sequence": 12,
            "correlation_id": "corr-omn-12935",
            "ticket_id": "OMN-12935",
            "observed_at": "2026-06-11T02:00:00Z",
        }
    )

    event = HandlerEvidenceDashboardEffect().normalize(delivered)

    # The real ids survive the envelope rather than collapsing to a cursor.
    assert event.correlation_id == "corr-omn-12935"
    assert event.event_id == "evt-blocked"
    assert event.ticket_id == "OMN-12935"
    assert event.topic == "onex.evt.omnimarket.readiness-gate-blocked.v1"
    assert event.normalized_stage == "READINESS_GATE_BLOCKED"

    db = InmemoryDatabaseAdapter()
    result = HandlerEvidenceDashboardReducer().project(event, db)
    replay = HandlerEvidenceDashboardReducer().project(event, db)

    assert result.rows_upserted == 3
    assert replay.rows_upserted == 3
    assert len(db.tables[DASHBOARD_TABLE]) == 1
    assert len(db.tables[TRACE_TABLE]) == 1
    assert len(db.tables[READINESS_TABLE]) == 1
    assert db.tables[TRACE_TABLE][0]["correlation_id"] == "corr-omn-12935"
    assert db.tables[READINESS_TABLE][0]["readiness_state"] == "BLOCKED"


def test_golden_chain_dashboard_effect_handle_emits_canonical_fields() -> None:
    delivered = _runtime_delivery(
        {
            "_topic": "onex.evt.omnimarket.evidence-collected.v1",
            "event_id": "evt-collected",
            "ingest_sequence": 10,
            "correlation_id": "corr-omn-12935",
        }
    )

    payload = HandlerEvidenceDashboardEffect().handle(delivered)

    assert payload["correlation_id"] == "corr-omn-12935"
    assert payload["event_id"] == "evt-collected"
    assert payload["normalized_status"] == "PASSED"
    assert payload["topic"] == "onex.evt.omnimarket.evidence-collected.v1"
