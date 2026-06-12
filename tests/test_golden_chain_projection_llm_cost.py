"""Golden chain tests for node_projection_llm_cost.

OMN-13001: this node now writes the ``llm_call_metrics`` per-call read model
(model_id, tokens, cost, usage_source-honest provenance), not the drifted
``llm_cost_aggregates`` schema. The aggregate read model is owned by
node_projection_cost_summary.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    HandlerProjectionLlmCost,
    ModelLlmCallCompletedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionLlmCost()
TABLE = "llm_call_metrics"


class TestLlmCostProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelLlmCallCompletedEvent(
            call_id="d498ad36-0000-0000-0000-000000000001",
            model_name="claude-opus-4-6",
            total_tokens=1500,
            prompt_tokens=1000,
            completion_tokens=500,
            estimated_cost_usd=0.045,
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        assert rows[0]["model_id"] == "claude-opus-4-6"
        assert rows[0]["total_tokens"] == 1500
        assert rows[0]["estimated_cost_usd"] == 0.045

    def test_upsert_by_input_hash(self) -> None:
        db = InmemoryDatabaseAdapter()
        # Same dedup dimensions => same input_hash => single row (idempotent).
        HANDLER.project(
            ModelLlmCallCompletedEvent(
                model_name="m", session_id="s", prompt_tokens=10, completion_tokens=5
            ),
            db,
        )
        HANDLER.project(
            ModelLlmCallCompletedEvent(
                model_name="m", session_id="s", prompt_tokens=10, completion_tokens=5
            ),
            db,
        )
        rows = db.query(TABLE)
        assert len(rows) == 1

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelLlmCallCompletedEvent(
                call_id=f"00000000-0000-0000-0000-{i:012d}",
                model_name="qwen3-coder-14b",
                session_id=f"sess-{i}",
                prompt_tokens=300 + i,
                completion_tokens=200,
                estimated_cost_usd=0.001,
            )
            for i in range(3)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 3
        assert len(db.query(TABLE)) == 3

    def test_usage_source_measured_maps_to_api(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelLlmCallCompletedEvent(
                model_name="m", session_id="s", total_tokens=10, usage_source="measured"
            ),
            db,
        )
        rows = db.query(TABLE)
        assert rows[0]["usage_source"] == "API"
        assert rows[0]["usage_is_estimated"] is False

    def test_usage_source_estimated(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelLlmCallCompletedEvent(
                model_name="m",
                session_id="s",
                total_tokens=10,
                usage_source="ESTIMATED",
            ),
            db,
        )
        rows = db.query(TABLE)
        assert rows[0]["usage_source"] == "ESTIMATED"
        assert rows[0]["usage_is_estimated"] is True

    def test_compute_cost_folds_into_estimated_cost(self, tmp_path: Path) -> None:
        manifest = tmp_path / "pricing_manifest.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0.0",
                    "models": {},
                    "compute_cost": {
                        "rtx_5090": {
                            "electricity_per_hour": 0.12,
                            "amortization_per_hour": 0.28,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        handler = HandlerProjectionLlmCost(pricing_manifest_path=manifest)
        db = InmemoryDatabaseAdapter()
        handler.project(
            ModelLlmCallCompletedEvent(
                model_name="qwen3-coder-30b-a3b",
                session_id="gpu",
                total_tokens=100,
                estimated_cost_usd=0.0,
                gpu_seconds=7200,
                gpu_type="rtx_5090",
                gpu_count=1,
                compute_usage_source="ESTIMATED",
            ),
            db,
        )
        row = db.query(TABLE)[0]
        # 7200s = 2h * (0.12 + 0.28) = 0.8 USD compute cost folded into the field.
        assert row["estimated_cost_usd"] == 0.8

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_llm_cost/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omniintelligence.llm-call-completed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )

    def test_contract_declares_llm_call_metrics_write_model(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_llm_cost/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        write_tables = [
            t["name"]
            for t in contract["db_io"]["db_tables"]
            if t.get("access") == "write"
        ]
        assert write_tables == [TABLE]
