"""Golden chain tests for node_projection_session_outcome."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_session_outcome.handlers.handler_projection_session_outcome import (
    HandlerProjectionSessionOutcome,
    ModelSessionOutcomeEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSessionOutcome()


class TestSessionOutcomeProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelSessionOutcomeEvent(
            session_id="sess-001",
            outcome="success",
            emitted_at="2026-04-06T10:00:00Z",
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("session_outcomes")
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-001"
        assert rows[0]["outcome"] == "success"

    def test_upsert_overwrites_same_session(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelSessionOutcomeEvent(session_id="sess-001", outcome="unknown"),
            db,
        )
        HANDLER.project(
            ModelSessionOutcomeEvent(session_id="sess-001", outcome="success"),
            db,
        )
        rows = db.query("session_outcomes")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelSessionOutcomeEvent(session_id=f"sess-{i:03d}", outcome="success")
            for i in range(5)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 5
        assert len(db.query("session_outcomes")) == 5

    def test_missing_emitted_at_uses_default(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelSessionOutcomeEvent(
            session_id="sess-002",
            outcome="failed",
        )
        HANDLER.project(event, db)
        rows = db.query("session_outcomes")
        assert rows[0]["emitted_at"] is not None

    def test_extra_fields_ignored(self) -> None:
        """Event model with extra='ignore' accepts unknown fields."""
        event = ModelSessionOutcomeEvent.model_validate(
            {"session_id": "sess-003", "outcome": "success", "extra_field": "ignored"}
        )
        assert event.session_id == "sess-003"

    def test_event_bus_wiring(self) -> None:
        contract_path = (
            "src/omnimarket/nodes/node_projection_session_outcome/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omniclaude.session-outcome.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert len(contract["event_bus"]["publish_topics"]) >= 1


class TestSessionOutcomeProjectionQuery:
    def test_query_by_outcome(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelSessionOutcomeEvent(session_id="s1", outcome="success"), db
        )
        HANDLER.project(ModelSessionOutcomeEvent(session_id="s2", outcome="failed"), db)
        results = db.query("session_outcomes", {"outcome": "failed"})
        assert len(results) == 1
        assert results[0]["session_id"] == "s2"
