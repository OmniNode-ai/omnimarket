"""Golden chain tests for node_projection_llm_cost."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    HandlerProjectionLlmCost,
    ModelLlmCallCompletedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionLlmCost()
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cost_observability"


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

    def test_compute_cost_projected_from_manifest_at_projection_time(
        self, tmp_path: Path
    ) -> None:
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
                call_id="gpu-call",
                model_name="qwen3-coder-30b-a3b",
                estimated_cost_usd=0.0,
                gpu_seconds=7200,
                gpu_type="rtx_5090",
                gpu_count=1,
                compute_usage_source="ESTIMATED",
            ),
            db,
        )

        row = db.query("llm_cost_aggregates")[0]
        assert row["total_cost_usd"] == 0.0
        assert row["compute_cost_usd"] == 0.8
        assert row["compute_usage_source"] == "ESTIMATED"

    def test_replay_task_7_gpu_fixture_matches_golden(self, tmp_path: Path) -> None:
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
                        },
                        "rtx_4090": {
                            "electricity_per_hour": 0.09,
                            "amortization_per_hour": 0.18,
                        },
                        "m2_ultra": {
                            "electricity_per_hour": 0.04,
                            "amortization_per_hour": 0.12,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        handler = HandlerProjectionLlmCost(pricing_manifest_path=manifest)
        db = InmemoryDatabaseAdapter()

        fixture_path = FIXTURE_DIR / "task-7-gpu.fixtures.jsonl"
        for line in fixture_path.read_text(encoding="utf-8").splitlines():
            handler.project(ModelLlmCallCompletedEvent(**json.loads(line)), db)

        actual = [
            {
                key: row.get(key)
                for key in (
                    "call_count",
                    "compute_cost_usd",
                    "compute_usage_source",
                    "completion_tokens",
                    "estimated_cost_usd",
                    "granularity",
                    "id",
                    "model_name",
                    "prompt_tokens",
                    "session_id",
                    "total_cost_usd",
                    "total_tokens",
                    "usage_source",
                )
            }
            for row in sorted(db.query("llm_cost_aggregates"), key=lambda r: r["id"])
        ]
        actual_json = json.dumps(actual, indent=2) + "\n"
        golden_json = (FIXTURE_DIR / "task-7-gpu.golden.json").read_text(
            encoding="utf-8"
        )

        assert actual_json == golden_json

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_llm_cost/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omniintelligence.llm-call-completed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
