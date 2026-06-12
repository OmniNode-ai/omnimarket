# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_context_roi (OMN-12955).

Proves the closure loop: context-ROI runner terminal event payload ->
context_roi_scores rows -> projection-API topic registration. The discovery
assertion is the headline fix: the /experiments panels failed with
``unknown_topic`` because no contract exposed
``onex.snapshot.projection.context.experiment-scores.v1``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.events.context_roi import (
    EnumFailureStage,
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
    ModelContextRoiRunCompletedEvent,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionContextRoi()
CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_context_roi/contract.yaml")
EXPERIMENT_SCORES_TOPIC = "onex.snapshot.projection.context.experiment-scores.v1"
RUNNER_TERMINAL_TOPIC = "onex.evt.omnimarket.context-roi-run-completed.v1"


def _row(
    *,
    correlation_id: str,
    subset: str = "golden_exemplar",
    model_id: str = "qwen3-coder-30b",
    first_pass: bool = True,
    final: bool = True,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> ModelAttemptReductionRow:
    return ModelAttemptReductionRow(
        run_id="run-001",
        correlation_id=correlation_id,
        task_id="task-A",
        run_order=1,
        context_factor_subset=subset,
        context_pack_hash="abc123",
        attempt_count=1,
        first_pass_success=first_pass,
        final_success=final,
        failure_stage=EnumFailureStage.NONE,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost=0.0,
        model_id=model_id,
        provider="local",
        endpoint_ref="local-coder",
    )


class TestContextRoiProjection:
    def test_projects_one_row_per_attempt_reduction_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(
                _row(correlation_id="c-1", subset="off", model_id="qwen3-coder-30b"),
                _row(correlation_id="c-2", subset="golden_exemplar"),
            ),
        )
        projection = HANDLER.project(result, db)
        assert projection.rows_upserted == 2

        rows = db.query("context_roi_scores")
        assert len(rows) == 2
        first = next(r for r in rows if r["correlation_id"] == "c-1")
        assert first["run_id"] == "run-001"
        assert first["context_factor_subset"] == "off"
        assert first["model_id"] == "qwen3-coder-30b"
        assert first["tokens_used"] == 150
        assert first["final_success"] is True
        assert first["failure_stage"] == "none"
        assert first["proof_class"] == "runtime-observed-only"

    def test_tokens_used_is_prompt_plus_completion(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(
                _row(
                    correlation_id="c-1",
                    prompt_tokens=320,
                    completion_tokens=80,
                ),
            ),
        )
        HANDLER.project(result, db)
        rows = db.query("context_roi_scores")
        assert rows[0]["tokens_used"] == 400

    def test_upsert_is_idempotent_on_correlation_id(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(_row(correlation_id="c-1", final=False),),
        )
        HANDLER.project(result, db)
        # Re-project the same correlation_id with a different outcome.
        result2 = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(_row(correlation_id="c-1", final=True),),
        )
        HANDLER.project(result2, db)
        rows = db.query("context_roi_scores")
        assert len(rows) == 1
        assert rows[0]["final_success"] is True

    def test_handle_strips_transport_metadata(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload: dict[str, object] = {
            "run_id": "run-001",
            "rows": [_row(correlation_id="c-1").model_dump(mode="json")],
            "_db": db,
            "_event_type": "context-roi-run-completed",
            "event_landed": "2026-06-11T00:00:00+00:00",
            "latency_ms": 12,
        }
        out = HANDLER.handle(payload)
        assert out["rows_upserted"] == 1
        rows = db.query("context_roi_scores")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "c-1"

    def test_inbound_event_model_round_trips_runner_payload(self) -> None:
        result = ModelContextRoiRunResult(
            run_id="run-001",
            rows=(_row(correlation_id="c-1"),),
        )
        event = ModelContextRoiRunCompletedEvent(
            **result.model_dump(mode="python", include={"run_id", "rows"})
        )
        assert event.run_id == "run-001"
        assert event.rows[0].correlation_id == "c-1"


class TestContextRoiContractWiring:
    def test_event_bus_wiring(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        assert (
            contract["handler"]["module"]
            == "omnimarket.nodes.node_projection_context_roi.handlers."
            "handler_projection_context_roi"
        )
        assert contract["handler"]["class"] == "HandlerProjectionContextRoi"
        assert RUNNER_TERMINAL_TOPIC in contract["event_bus"]["subscribe_topics"]
        assert (
            contract["event_bus"]["consumer_group"]
            == "local.omnimarket.node_projection_context_roi.consume.v1"
        )

    def test_migration_declares_handler_schema(self) -> None:
        migration = (
            CONTRACT_PATH.parent / "migrations" / "001_create_context_roi_scores.sql"
        ).read_text()
        assert "CREATE TABLE IF NOT EXISTS context_roi_scores" in migration
        assert "correlation_id TEXT NOT NULL" in migration
        assert "ux_context_roi_scores_identity" in migration
        assert "trg_context_roi_scores_updated_at" in migration
        assert "NEW.updated_at = NOW()" in migration

    def test_projection_api_exposes_experiment_scores_topic(self) -> None:
        """The headline fix: discovery must register the panel topic.

        Without this exposure the /experiments hero + heatmap panels resolve to
        ``unknown_topic`` at the projection API.
        """
        topic_map = build_projection_topic_map()
        assert EXPERIMENT_SCORES_TOPIC in topic_map, (
            f"{EXPERIMENT_SCORES_TOPIC} not registered by projection discovery; "
            "/experiments panels would still fail with unknown_topic"
        )
        cfg = topic_map[EXPERIMENT_SCORES_TOPIC]
        assert cfg.table == "context_roi_scores"
        assert cfg.source_contract == "projection_context_roi"
        # Columns the dashboard heatmap depends on must be exposed.
        for required_column in (
            "model_id",
            "context_factor_subset",
            "final_success",
            "tokens_used",
            "run_id",
        ):
            assert required_column in cfg.columns
