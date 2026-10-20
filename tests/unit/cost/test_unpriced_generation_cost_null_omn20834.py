# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20834: a generation run on an unpriced model records a null cost, not 0.0.

When the cost-pricing surface reports a model as UNKNOWN, the run was not
priced. Recording it as 0.0 made it indistinguishable from a run that was
priced and really cost nothing (a free local model). Failure modes this module
is written against:

- AC1: ``_calculate_cost`` coerces UNKNOWN to 0.0, or the generation benchmark
  cannot carry a null cost;
- AC2: the generation_events projection stores 0 for the unpriced run, or a
  priced run's stored cost changes;
- AC3: the context-ROI runner counts the unpriced run in its report at 0.0
  instead of leaving it out.

The Postgres half of AC2 (the column accepts NULL) is
tests/test_omn20834_generation_cost_null_real_postgres.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    _calculate_cost,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationBenchmark,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)

_UNPRICED_MODEL = "omn20834-model-not-in-pricing"
_PRICED_MODEL = "gemini-2.5-flash"

_ROI_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)


# ---------------------------------------------------------------------------
# AC1: the generation run records null
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_unknown_cloud_model_costs_null_not_zero() -> None:
    assert _calculate_cost("cloud", _UNPRICED_MODEL, 1_000, 1_000) is None


@pytest.mark.unit
def test_a_priced_run_and_a_free_local_run_keep_their_costs() -> None:
    priced = _calculate_cost("cloud", _PRICED_MODEL, 12_000, 3_000)
    assert priced is not None
    assert priced > 0.0
    assert _calculate_cost("local", "Qwen3.6-35B-A3B", 10_000, 5_000) == 0.0


@pytest.mark.unit
def test_the_generation_benchmark_carries_a_null_cost() -> None:
    fields: dict[str, Any] = {
        "correlation_id": "omn20834-benchmark",
        "task_description": "generate a node",
        "provider": "cloud",
        "model_id": _UNPRICED_MODEL,
        "endpoint_class": "cheap_cloud",
        "attempt_count": 1,
        "total_latency_e2e_ms": 10,
        "contract_passed": True,
    }
    benchmark = ModelGenerationBenchmark(**fields, cost_inference_usd=None)
    assert benchmark.cost_inference_usd is None
    assert json.loads(benchmark.model_dump_json())["cost_inference_usd"] is None
    # A payload that names no cost decodes as unpriced, never as free.
    assert ModelGenerationBenchmark(**fields).cost_inference_usd is None


# ---------------------------------------------------------------------------
# AC2: the generation_events projection stores null, a priced cost unchanged
# ---------------------------------------------------------------------------


class _RecordingDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        self.rows.append({"_table": table, **row})
        return True

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        return []


def _project(correlation_id: str, model_id: str, cost: float | None) -> object:
    db = _RecordingDB()
    HandlerProjectionDelegation().handle(
        {
            "_db": db,
            "_event_type": "onex.evt.omnimarket.node-generation-completed.v1",
            "correlation_id": correlation_id,
            "task_description": "generate a node",
            "provider": "cloud",
            "model_id": model_id,
            "endpoint_class": "cheap_cloud",
            "attempt_count": 1,
            "total_latency_e2e_ms": 10,
            "contract_passed": True,
            "cost_inference_usd": cost,
        }
    )
    assert len(db.rows) == 1
    assert db.rows[0]["_table"] == "generation_events"
    return db.rows[0]["cost_inference_usd"]


@pytest.mark.unit
def test_the_projection_stores_null_for_the_unpriced_run() -> None:
    cost = _calculate_cost("cloud", _UNPRICED_MODEL, 1_000, 1_000)
    assert _project("omn20834-unpriced", _UNPRICED_MODEL, cost) is None


@pytest.mark.unit
def test_the_projection_stores_a_priced_run_unchanged() -> None:
    cost = _calculate_cost("cloud", _PRICED_MODEL, 12_000, 3_000)
    assert cost is not None
    assert _project("omn20834-priced", _PRICED_MODEL, cost) == cost


# ---------------------------------------------------------------------------
# AC3: the context-ROI report excludes the unpriced run
# ---------------------------------------------------------------------------


def _terminal(model_id: str, cost: float | None) -> dict[str, Any]:
    return {
        "attempt_count": 1,
        "contract_passed": True,
        "first_pass_success": True,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_inference_usd": cost,
        "model_id": model_id,
        "provider": "cloud",
        "endpoint_class": "cheap_cloud",
    }


@pytest.mark.unit
def test_the_roi_report_excludes_the_unpriced_run() -> None:
    priced_cost = _calculate_cost("cloud", _PRICED_MODEL, 100, 50)
    assert priced_cost is not None
    by_task = {
        "unpriced task": _terminal(
            _UNPRICED_MODEL, _calculate_cost("cloud", _UNPRICED_MODEL, 100, 50)
        ),
        "priced task": _terminal(_PRICED_MODEL, priced_cost),
    }
    published: list[dict[str, Any]] = []

    def _publish(topic: str, payload: bytes) -> None:
        published.append(json.loads(payload))

    def _consume(topic: str, correlation_id: str, timeout: float) -> dict[str, Any]:
        command = next(p for p in published if p["correlation_id"] == correlation_id)
        return {
            **by_task[command["task_description"]],
            "correlation_id": correlation_id,
        }

    runner = HandlerContextRoiRunner(
        event_publisher=_publish,
        event_consumer=_consume,
        runner_contract_path=_ROI_CONTRACT,
    )
    result = runner.handle(
        ModelContextRoiRunRequest(
            run_id="omn20834-roi",
            tasks=(
                ModelContextRoiTask(
                    task_id="unpriced", task_description="unpriced task"
                ),
                ModelContextRoiTask(task_id="priced", task_description="priced task"),
            ),
            arms=(ModelContextRoiArmSpec(label="off", factor_subset=()),),
            trials_per_cell=1,
            max_attempts=1,
            arm_order_seed=1,
            generation_timeout_seconds=1.0,
            contract_hash="omn20834",
        )
    )

    assert [row.task_id for row in result.rows] == ["priced"]
    assert result.rows[0].estimated_cost == priced_cost
    assert result.total_trials == 1
    assert result.failed_trials == 0
    assert result.excluded_unpriced_trials == 1
    assert any("unpriced" in warning for warning in result.warnings)
