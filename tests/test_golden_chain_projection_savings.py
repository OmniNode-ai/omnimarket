"""Golden chain tests for node_projection_savings."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
    ModelSavingsEstimatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSavings()


class TestSavingsProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelSavingsEstimatedEvent(
            source_event_id="sev-001",
            session_id="sess-001",
            actual_total_tokens=10000,
            actual_cost_usd=0.30,
            direct_savings_usd=0.15,
            direct_tokens_saved=5000,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert rows[0]["direct_savings_usd"] == 0.15

    def test_upsert_by_source_event_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelSavingsEstimatedEvent(
                source_event_id="sev-001",
                session_id="s1",
                direct_savings_usd=0.10,
            ),
            db,
        )
        HANDLER.project(
            ModelSavingsEstimatedEvent(
                source_event_id="sev-001",
                session_id="s1",
                direct_savings_usd=0.20,
            ),
            db,
        )
        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert rows[0]["direct_savings_usd"] == 0.20

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelSavingsEstimatedEvent(
                source_event_id=f"sev-{i:03d}",
                session_id="sess-batch",
                direct_savings_usd=float(i),
            )
            for i in range(4)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 4

    def test_optional_fields_default(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelSavingsEstimatedEvent(
            source_event_id="sev-minimal",
            session_id="sess-min",
        )
        HANDLER.project(event, db)
        rows = db.query("savings_estimates")
        assert rows[0]["actual_total_tokens"] == 0
        assert rows[0]["categories"] is None

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_savings/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omnibase-infra.savings-estimated.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
