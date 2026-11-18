# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC4, AC6, AC7: the local views agree on one pass rate, one run count and the model that ran.

Found on a copy of an operator's store (2026-11-15, 3,682 runs): quality_gate_passed is NOT NULL DEFAULT 0, so the 168
runs whose content quality was never evaluated (quality_gates_checked = 0) were counted as failed gate checks;
quality-gate.v1 served 84.4% where the evaluated rate is 88.4%. The summary view's ``data_source <> 'experiment'``
dropped the 170 runs whose data_source is NULL. model-routing.v1 grouped by the delegated_to alias, so one model
appeared twice (Beam-1.0-501B-A23B and byok-reflection). daily.v1 tested ``IS NULL`` on columns that cannot be NULL.

Each test names the failure it exists to catch:

* a never-evaluated run counted as a failed gate check (quality-gate, summary, model-routing);
* a NULL-data_source run dropped from the summary's run count;
* the three views serving different run counts or pass rates for the same store;
* one model split across its backend alias and its name;
* daily.v1 counting a never-evaluated run as failed, and a run with no tokens as measured.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_ROUTING = "onex.snapshot.projection.delegation.model-routing.v1"
_QUALITY = "onex.snapshot.projection.delegation.quality-gate.v1"
_SUMMARY = "onex.snapshot.projection.delegation.summary.v1"
_DAILY = "onex.snapshot.projection.delegation.daily.v1"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_TOPICS = {
    cfg.topic: cfg
    for cfg in load_projection_exposures_from_contract(
        yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
        "node_projection_delegation",
        _CONTRACT,
    )
}


def _run(correlation_id: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "reasoning",
        "delegated_to": "Qwen3.8-27B",
        "model_name": "Qwen3.8-27B",
        "cost_tier_name": "local",
        "quality_gate_passed": True,
        "quality_gates_checked": 1,
        "terminal_ok": True,
        "latency_ms": 900,
        "tokens_input": 100,
        "tokens_output": 20,
        "data_source": "real",
        "created_at": "2026-11-14T10:00:00+00:00",
    }
    row.update(fields)
    return row


# The store copy's shape in miniature: two evaluated runs (one passed, one failed), one run the gate never evaluated
# (stored failed by the column default), and one run whose data_source is NULL.
_RUNS = (
    _run("passed"),
    _run("failed", quality_gate_passed=False),
    _run("not-evaluated", quality_gate_passed=False, quality_gates_checked=0),
    _run("null-source", data_source=None),
)


def _store(tmp_path: Path, *rows: dict[str, Any]) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert("delegation_events", "correlation_id", row)
    return path


def _row(path: Path, topic: str) -> dict[str, Any]:
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=topic, tenant_id=_TENANT))
    )
    assert result.http_status == 200, result.response
    body = dict(result.response or {})
    assert body["row_count"] == 1, body
    row: dict[str, Any] = body["rows"][0]
    return row


def test_quality_gate_counts_only_evaluated_runs(tmp_path: Path) -> None:
    row = _row(_store(tmp_path, *_RUNS), _QUALITY)
    assert row["total_checks"] == 3
    assert row["total_failed"] == 1
    assert row["overall_pass_rate"] == pytest.approx(2 / 3)
    # The contract pins the view's columns (Postgres serves the same exposure), so the count rides in the declared
    # by_check_type JSON.
    [check] = row["by_check_type"]
    assert (
        check["passed"],
        check["failed"],
        check["total"],
        check["not_evaluated"],
    ) == (2, 1, 3, 1)


def test_summary_keeps_a_null_data_source_run_and_counts_only_evaluated_runs(
    tmp_path: Path,
) -> None:
    row = _row(_store(tmp_path, *_RUNS), _SUMMARY)
    assert row["totalDelegations"] == 4
    assert row["qualityGateTotal"] == 3
    assert row["qualityGatePassRate"] == pytest.approx(2 / 3)
    # Not-evaluated runs are the difference between the run count and the evaluated total.
    assert row["totalDelegations"] - row["qualityGateTotal"] == 1


def test_an_experiment_run_stays_out_of_every_view(tmp_path: Path) -> None:
    path = _store(tmp_path, *_RUNS, _run("synthetic", data_source="experiment"))
    assert _row(path, _SUMMARY)["totalDelegations"] == 4
    assert _row(path, _ROUTING)["total_delegations"] == 4
    assert _row(path, _QUALITY)["total_checks"] == 3


def test_the_three_views_serve_one_run_count_and_one_pass_rate(tmp_path: Path) -> None:
    path = _store(tmp_path, *_RUNS)
    summary, routing, quality = (
        _row(path, _SUMMARY),
        _row(path, _ROUTING),
        _row(path, _QUALITY),
    )
    assert summary["totalDelegations"] == routing["total_delegations"] == 4
    assert summary["qualityGatePassRate"] == pytest.approx(quality["overall_pass_rate"])
    [model] = routing["by_model"]
    assert model["qg_pass_rate"] == pytest.approx(quality["overall_pass_rate"])


def test_one_model_reached_through_a_backend_is_one_group(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _run(
            "beam-direct",
            delegated_to="Beam-1.0-501B-A23B",
            model_name="Beam-1.0-501B-A23B",
        ),
        _run(
            "beam-byok-1",
            delegated_to="byok-reflection",
            model_name="Beam-1.0-501B-A23B",
        ),
        _run(
            "beam-byok-2",
            delegated_to="byok-reflection",
            model_name="Beam-1.0-501B-A23B",
        ),
    )
    routing = _row(path, _ROUTING)
    assert [(m["model_name"], m["total_count"]) for m in routing["by_model"]] == [
        ("Beam-1.0-501B-A23B", 3)
    ]
    summary = _row(path, _SUMMARY)
    assert [(m["model"], m["count"]) for m in summary["byModel"]] == [
        ("Beam-1.0-501B-A23B", 3)
    ]


def test_daily_counts_a_never_evaluated_run_as_unrecorded_and_a_run_without_tokens_as_unmeasured(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, *_RUNS, _run("no-tokens", tokens_input=0, tokens_output=0))
    [day] = _row(path, _DAILY)["days"]
    assert day["runs"] == 5
    assert day["unrecorded"] == 1
    assert day["failed"] == 1
    assert day["passed"] == 3
    assert day["tokensInUnmeasured"] == 1
    assert day["tokensOutUnmeasured"] == 1
