"""Golden chain tests for node_projection_llm_cost."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    HandlerProjectionLlmCost,
    ModelLlmCallCompletedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionLlmCost()


class TestLlmCostProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelLlmCallCompletedEvent(
            call_id="call-001",
            model_name="claude-opus-4-6",
            total_tokens=1500,
            prompt_tokens=1000,
            completion_tokens=500,
            estimated_cost_usd=0.045,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("llm_cost_aggregates")
        assert len(rows) == 1
        assert rows[0]["model_name"] == "claude-opus-4-6"
        assert rows[0]["total_tokens"] == 1500

    def test_upsert_by_call_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelLlmCallCompletedEvent(call_id="call-001", total_tokens=100), db
        )
        HANDLER.project(
            ModelLlmCallCompletedEvent(call_id="call-001", total_tokens=200), db
        )
        rows = db.query("llm_cost_aggregates")
        assert len(rows) == 1
        assert rows[0]["total_tokens"] == 200

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelLlmCallCompletedEvent(
                call_id=f"call-{i:03d}",
                model_name="qwen3-coder-14b",
                total_tokens=500,
                estimated_cost_usd=0.001,
            )
            for i in range(3)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 3

    def test_usage_source_preserved(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelLlmCallCompletedEvent(
                call_id="call-est", usage_source="ESTIMATED", total_tokens=0
            ),
            db,
        )
        rows = db.query("llm_cost_aggregates")
        assert rows[0]["usage_source"] == "ESTIMATED"

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_llm_cost/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omniintelligence.llm-call-completed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
