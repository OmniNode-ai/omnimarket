# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23628: a missing overlay key reads as an explicit not-configured line.

The class line has no packaged value. When no overlay supplies the key
``delegation.capability_map.line``, nothing refuses: the read carries the line
None with a reason naming the key, a map run keeps its rows and assignments
with the line None, and the fold makes no cell while still counting every
admitted row. The wire shape drops the line key only when it is unset.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_capability_line import resolve_capability_line
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_grid import (
    ModelCapabilityGridGrade,
    ModelCapabilityGridItem,
    ModelCapabilityGridModel,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_grid_run import (
    build_grid_map_run,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_run import (
    build_map_run,
)
from tests.nodes.node_delegation_capability_map_compute.test_cells_from_code_eval_run import (
    _OBSERVED_AT,
    _TENANT,
    _observations,
    _run_and_result,
    _write_overlay,
)

pytestmark = pytest.mark.unit

_LINE = ModelCapabilityLine(floor=0.50, margin=0.07, confidence=0.95, power=0.8)


def test_an_absent_key_reads_a_not_configured_line_and_raises_nothing() -> None:
    reading = resolve_capability_line(environ={})
    assert reading.line is None
    assert reading.not_configured_reason is not None
    assert "delegation.capability_map.line" in reading.not_configured_reason


def test_a_configured_overlay_reads_the_line_and_no_reason(tmp_path) -> None:
    reading = resolve_capability_line(_write_overlay(tmp_path, 0.50))
    assert reading.not_configured_reason is None
    assert reading.line is not None
    assert reading.line.floor == 0.50


def test_a_broken_overlay_still_refuses(tmp_path) -> None:
    """A present overlay that does not validate refuses, it does not read absent."""
    environ = _write_overlay(tmp_path, 0.50)
    (
        Path(environ["ONEX_SKILL_OVERLAY_ROOTS"])
        / "node_projection_delegation_capability_map"
        / "overlay.yaml"
    ).write_text(
        "line:\n  floor: 0.50\n  margin: 0.07\n  confidence: 0.95\n  power: 0.8\n"
        "unknown: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError):
        resolve_capability_line(environ)


def test_a_run_without_a_line_keeps_its_rows_and_assignments() -> None:
    run, _ = _run_and_result(_observations(), None)
    assert run.line is None
    assert run.rows
    assert run.assignments


def test_the_fold_without_a_line_counts_rows_but_makes_no_cell(tmp_path) -> None:
    run, result = _run_and_result(_observations(), None)
    assert result.cells == ()
    assert result.best_configurations == ()
    assert result.line_not_configured is True
    assert result.folded_row_count == len(run.rows)
    line = resolve_capability_line(_write_overlay(tmp_path, 0.50)).line
    _, with_line_result = _run_and_result(_observations(), line)
    assert with_line_result.line_not_configured is False
    assert len(with_line_result.cells) == 4
    assert result.folded_row_count == with_line_result.folded_row_count


def test_the_wire_shape_drops_the_line_key_only_when_unconfigured() -> None:
    run, _ = _run_and_result(_observations(), None)
    assert "line" not in run.model_dump(mode="json")
    assert (
        ModelCapabilityMapRunRecorded.model_validate_json(run.model_dump_json()).line
        is None
    )
    with_line, _ = _run_and_result(_observations(), _LINE)
    dumped = with_line.model_dump(mode="json")
    assert dumped["line"]["floor"] == 0.50
    assert (
        ModelCapabilityMapRunRecorded.model_validate_json(with_line.model_dump_json())
        == with_line
    )


def test_a_payload_without_the_line_key_validates() -> None:
    with_line, _ = _run_and_result(_observations(), _LINE)
    payload = with_line.model_dump(mode="json")
    del payload["line"]
    assert ModelCapabilityMapRunRecorded.model_validate(payload).line is None


def test_the_line_model_still_has_no_default() -> None:
    with pytest.raises(ValidationError):
        ModelCapabilityLine()


def test_the_lab_producers_build_runs_without_an_overlay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded and graded lab runs build with the line None when no overlay configures it."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    items = [
        {
            "item_key": f"code_review-{i}:0",
            "task_class": "code_review",
            "label": "adequate",
            "prompt_snapshot": "Review this hunk 0: def f(x): return x + 1",
            "observed_at": _OBSERVED_AT,
            "rubric_version": "ev4-blind-v1",
            "rater_role": "blind_model:claude-opus-5-5",
            "model_key": "qwen3-coder-30b",
            "backend_id": "local-url",
        }
        for i in range(3)
    ]
    run, skipped = build_map_run(_TENANT, items)
    assert skipped == {}
    assert run.line is None
    assert run.rows
    model = ModelCapabilityGridModel(
        model_key="Qwen3.8-27B", backend_id="local-coder", model_revision="Qwen3.8-27B"
    )
    grid_items = tuple(
        ModelCapabilityGridItem(
            item_key=f"item-{i}",
            task_class="summarization",
            size_band=EnumSizeBand.S,
            prompt=f"summarize note {i}",
        )
        for i in range(3)
    )
    grid_run, counts = build_grid_map_run(
        tenant_id=_TENANT,
        items=grid_items,
        models=(model,),
        configuration=ModelCapabilityConfiguration(),
        grades={
            (item.item_key, model.backend_id): ModelCapabilityGridGrade(
                passed=True,
                host="model-host.example",
                answering_backend="local-coder",
                served_model="Qwen3.8-27B",
            )
            for item in grid_items
        },
        suite_revision="delegation-class-rubrics.v1",
        grader_revision="rubric_check",
        observed_at=_OBSERVED_AT,
    )
    assert counts["verdict_rows"] == 3
    assert grid_run.line is None
