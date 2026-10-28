# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21434: a graded run records the machine that served each answer.

The labelled items predate the backend record, so every row they make is
refused as identity_field_missing and the map writes no cell. A graded grid run
calls each backend's endpoint itself, so each verdict row names the backend it
asked, the host that endpoint lives on and the backend that answered. Those
rows fold into cells; an answer from another model or no recorded answering
backend still folds nowhere.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.enum_capability_inadmissible_reason import (
    EnumCapabilityInadmissibleReason,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_grid import (
    ModelCapabilityGridGrade,
    ModelCapabilityGridItem,
    ModelCapabilityGridModel,
    ModelCapabilityGridRequest,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerCapabilityGridRun,
    HandlerDelegationCapabilityMap,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_grid_run import (
    GRID_HARNESS_REVISION,
    ModelGridBackend,
    build_grid_map_run,
    call_grid_backend,
    grid_items,
    row_identity_gaps,
)
from omnimarket.ranges.power import required_sample_size

pytestmark = pytest.mark.unit

_TENANT = UUID("22222222-2222-2222-2222-222222222222")
_DEFAULT = ModelCapabilityConfiguration()
_REQUIRED_N = required_sample_size(floor=0.90, margin=0.07, confidence=0.95, power=0.8)
_MODEL = ModelCapabilityGridModel(
    model_key="Qwen3.8-27B", backend_id="local-coder", model_revision="Qwen3.8-27B"
)


def _request(n_items: int) -> ModelCapabilityGridRequest:
    return ModelCapabilityGridRequest(
        tenant_id=_TENANT,
        items=tuple(
            ModelCapabilityGridItem(
                item_key=f"item-{i}",
                task_class="summarization",
                size_band=EnumSizeBand.S,
                prompt=f"summarize note {i}",
            )
            for i in range(n_items)
        ),
        models=(_MODEL,),
        configurations=(_DEFAULT,),
        suite_revision="delegation-class-rubrics.v1",
        grader_revision="rubric_check",
        harness_revision=GRID_HARNESS_REVISION,
    )


def _served(
    answering_backend: str | None = "local-coder", served_model: str | None = None
):
    def grade(item, model, configuration) -> ModelCapabilityGridGrade:
        del item, configuration
        return ModelCapabilityGridGrade(
            passed=True,
            host="model-host.example",
            answering_backend=answering_backend,
            served_model=served_model or model.model_revision,
        )

    return grade


def _fold(rows, request: ModelCapabilityGridRequest):
    from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
        ModelCapabilityMapFoldRequest,
    )
    from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
        ModelCapabilityServingAssignment,
    )

    return HandlerDelegationCapabilityMap().handle(
        ModelCapabilityMapFoldRequest(
            map_run_id="graded-1",
            rows=tuple(rows),
            assignments=tuple(
                ModelCapabilityServingAssignment(
                    model_key=m.model_key,
                    backend_id=m.backend_id,
                    configuration_id=c.configuration_id,
                    identity=request.identity_for(m, c),
                )
                for m in request.models
                for c in request.configurations
            ),
        )
    )


def test_graded_run_rows_record_backend_host_and_answering_backend() -> None:
    rows = HandlerCapabilityGridRun(_served()).handle(_request(5))
    assert len(rows) == 5
    for row in rows:
        assert row.backend_id == "local-coder"
        assert row.host == "model-host.example"
        assert row.answering_backend == "local-coder"
        assert row_identity_gaps(row) == ()


def test_graded_run_rows_fold_into_cells_and_none_is_identity_field_missing() -> None:
    request = _request(_REQUIRED_N + 5)
    result = _fold(HandlerCapabilityGridRun(_served()).handle(request), request)
    assert len(result.cells) == 1
    assert result.cells[0].verdict is EnumCapabilityCellVerdict.MET
    assert result.cells[0].backend_id == "local-coder"
    assert result.inadmissible == ()


def test_graded_run_answer_from_another_backend_is_identity_mismatch() -> None:
    request = _request(4)
    rows = HandlerCapabilityGridRun(_served("local-studio-planner")).handle(request)
    result = _fold(rows, request)
    assert result.cells == ()
    assert {e.reason: e.count for e in result.inadmissible} == {
        EnumCapabilityInadmissibleReason.IDENTITY_MISMATCH: 4
    }


def test_graded_run_answer_from_another_model_is_identity_mismatch() -> None:
    request = _request(3)
    rows = HandlerCapabilityGridRun(_served(served_model="qwen2.5-coder:7b")).handle(
        request
    )
    result = _fold(rows, request)
    assert result.cells == ()
    assert {e.reason: e.count for e in result.inadmissible} == {
        EnumCapabilityInadmissibleReason.IDENTITY_MISMATCH: 3
    }


def test_graded_run_with_no_answering_backend_is_identity_field_missing() -> None:
    request = _request(3)
    rows = HandlerCapabilityGridRun(_served(None)).handle(request)
    assert all(row_identity_gaps(row) == ("answering_backend",) for row in rows)
    result = _fold(rows, request)
    assert result.cells == ()
    assert {e.reason: e.count for e in result.inadmissible} == {
        EnumCapabilityInadmissibleReason.IDENTITY_FIELD_MISSING: 3
    }


def test_call_grid_backend_records_the_endpoint_host_and_the_served_model() -> None:
    backend = ModelGridBackend(
        model_key="Qwen3.8-27B",
        backend_id="local-coder",
        endpoint_url="http://model-host.example:8000/v1/chat/completions",
        served_model_id="Qwen3.8-27B",
    )
    sent: list[dict[str, Any]] = []

    def post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append({"url": url, "body": body, "timeout": timeout})
        return {
            "model": "Qwen3.8-27B",
            "choices": [{"message": {"content": "<think>x</think>The answer."}}],
        }

    answer = call_grid_backend(backend, "summarize this", timeout=30.0, post=post)
    assert sent[0]["url"] == backend.endpoint_url
    assert sent[0]["body"]["model"] == "Qwen3.8-27B"
    assert answer.host == "model-host.example"
    assert answer.answering_backend == "local-coder"
    assert answer.served_model == "Qwen3.8-27B"
    assert answer.answer_text == "The answer."


def test_grid_items_band_each_distinct_prompt_once() -> None:
    raw = [
        {"item_key": "b", "task_class": "summarization", "prompt_snapshot": "same"},
        {"item_key": "a", "task_class": "summarization", "prompt_snapshot": "same"},
        {"item_key": "c", "task_class": "no_such_class", "prompt_snapshot": "x"},
    ]
    items, skipped = grid_items(raw)
    assert [i.item_key for i in items] == ["a"]
    assert items[0].size_band is EnumSizeBand.S
    assert skipped == {"duplicate_prompt": 1, "size_band_task_class_unavailable": 1}


def test_build_grid_map_run_writes_rows_whose_three_serving_fields_are_non_empty() -> (
    None
):
    request = _request(_REQUIRED_N + 2)
    grades = {
        (item.item_key, _MODEL.backend_id): ModelCapabilityGridGrade(
            passed=True,
            host="model-host.example",
            answering_backend="local-coder",
            served_model="Qwen3.8-27B",
        )
        for item in request.items
    }
    run, counts = build_grid_map_run(
        tenant_id=_TENANT,
        items=request.items,
        models=(_MODEL,),
        configuration=_DEFAULT,
        grades=grades,
        suite_revision="delegation-class-rubrics.v1",
        grader_revision="rubric_check",
        observed_at=datetime(2026, 10, 28, tzinfo=UTC),
    )
    assert counts["verdict_rows"] == _REQUIRED_N + 2
    assert counts["rows_missing_serving_field"] == 0
    for row in run.rows:
        dumped = json.loads(row.model_dump_json())
        assert dumped["backend_id"]
        assert dumped["host"]
        assert dumped["answering_backend"]
    result = HandlerDelegationCapabilityMap().handle(run.fold_request())
    assert len(result.cells) == 1
    assert result.inadmissible == ()


def test_grid_run_command_writes_rows_with_the_three_serving_fields(
    tmp_path, monkeypatch
) -> None:
    from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
        ModelCapabilityMapRunRecorded,
    )
    from omnimarket.nodes.node_projection_delegation_capability_map import (
        lab_grid_run,
    )

    items = tmp_path / "items.json"
    items.write_text(
        json.dumps(
            [
                {
                    "item_key": f"k{i}",
                    "task_class": "summarization",
                    "prompt_snapshot": f"Summarize release note {i}.",
                }
                for i in range(3)
            ]
        )
    )
    backends = tmp_path / "backends.json"
    backends.write_text(
        json.dumps(
            [
                {
                    "model_key": "Qwen3.8-27B",
                    "backend_id": "local-coder",
                    "endpoint_url": "http://model-host.example:8000/v1/chat/completions",
                    "served_model_id": "Qwen3.8-27B",
                }
            ]
        )
    )

    def post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        del url, timeout
        return {
            "model": body["model"],
            "choices": [{"message": {"content": "The release adds a cache."}}],
        }

    monkeypatch.setattr(lab_grid_run, "_http_post", post)
    rows_out, run_out = tmp_path / "rows.jsonl", tmp_path / "run.json"
    assert (
        lab_grid_run.main(
            [
                "--tenant-id",
                str(_TENANT),
                "grid-run",
                "--items-file",
                str(items),
                "--backends-file",
                str(backends),
                "--rows-out",
                str(rows_out),
                "--run-out",
                str(run_out),
                "--no-write",
            ]
        )
        == 0
    )
    rows = [json.loads(line) for line in rows_out.read_text().splitlines()]
    assert len(rows) == 3
    empty = [
        r
        for r in rows
        if not (r["backend_id"] and r["host"] and r["answering_backend"])
    ]
    assert empty == []
    run = ModelCapabilityMapRunRecorded.model_validate_json(run_out.read_text())
    assert len(run.rows) == 3
    assert {a.backend_id for a in run.assignments} == {"local-coder"}


def test_deploy_probe_the_packaged_grid_handler_and_lab_command_record_serving_identity() -> (
    None
):
    """What a deployed omnimarket ships: the grid handler takes a grade naming
    the serving machine, and the lab command module is importable as
    ``python -m`` with its ``main``."""
    import importlib
    import inspect

    from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
        handler_capability_grid_run,
    )

    assert "ModelCapabilityGridGrade" in inspect.getsource(handler_capability_grid_run)
    module = importlib.import_module(
        "omnimarket.nodes.node_projection_delegation_capability_map.lab_grid_run"
    )
    assert callable(module.main)
    rows = HandlerCapabilityGridRun(_served()).handle(_request(1))
    assert (rows[0].host, rows[0].answering_backend) == (
        "model-host.example",
        "local-coder",
    )
