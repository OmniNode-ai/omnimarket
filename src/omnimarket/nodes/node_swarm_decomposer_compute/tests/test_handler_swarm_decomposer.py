# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_swarm_decomposer_compute.handlers.handler_swarm_decomposer import (
    HandlerSwarmDecomposer,
    validate_decomposition,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.enums import (
    EnumDecompositionStatus,
    EnumSubtaskCategory,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_subtask import (
    ModelSubtask,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_swarm_decompose_request import (
    ModelSwarmDecomposeRequest,
)

_VALID_PLANNER_OUTPUT = json.dumps(
    {
        "subtasks": [
            {
                "description": "Analyse requirements",
                "model_affinity": "ep-1",
                "depends_on": [],
                "estimated_tokens": 200,
                "token_estimation_method": "char_ratio",
                "category": "analysis",
            },
            {
                "description": "Write implementation",
                "model_affinity": "ep-2",
                "depends_on": ["0"],
                "estimated_tokens": 800,
                "token_estimation_method": "char_ratio",
                "category": "code",
            },
        ]
    }
)


def _make_request(**overrides: object) -> ModelSwarmDecomposeRequest:
    defaults: dict[str, object] = {
        "planner_output": _VALID_PLANNER_OUTPUT,
        "planner_model_id": "model-x",
        "planner_output_hash": "abc123hash",
        "endpoint_ids": ("ep-1", "ep-2"),
        "original_task": "x" * 3000,
        "token_threshold": 2000,
    }
    defaults.update(overrides)
    return ModelSwarmDecomposeRequest(**defaults)


# ---------------------------------------------------------------------------
# Successful decomposition
# ---------------------------------------------------------------------------


def test_handler_success_parses_subtasks() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request())

    assert result.status == EnumDecompositionStatus.SUCCEEDED
    assert len(result.decomposition.subtasks) == 2
    assert result.decomposition.subtasks[0].category == EnumSubtaskCategory.ANALYSIS
    assert result.decomposition.subtasks[1].category == EnumSubtaskCategory.CODE


def test_handler_success_depends_on_resolved_to_ids() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request())

    st0 = result.decomposition.subtasks[0]
    st1 = result.decomposition.subtasks[1]
    assert st1.depends_on == (st0.subtask_id,)


def test_handler_success_model_fields_populated() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request())

    assert result.decomposition.decomposition_model == "model-x"
    assert result.decomposition.original_task == "x" * 3000
    assert len(result.decomposition.original_task_hash) == 16
    assert result.decomposition.decomposition_run_id != ""


def test_handler_success_correlation_id_preserved() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request(correlation_id="corr-abc"))

    assert result.decomposition.correlation_id == "corr-abc"


def test_handler_auto_generates_correlation_id_when_empty() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request(correlation_id=""))

    assert result.decomposition.correlation_id != ""


# ---------------------------------------------------------------------------
# Unknown model_affinity fallback
# ---------------------------------------------------------------------------


def test_handler_unknown_affinity_cleared() -> None:
    payload = json.dumps(
        {
            "subtasks": [
                {
                    "description": "step",
                    "model_affinity": "unknown-ep",
                    "depends_on": [],
                    "estimated_tokens": 0,
                    "token_estimation_method": "char_ratio",
                    "category": "general",
                }
            ]
        }
    )
    handler = HandlerSwarmDecomposer()
    result = handler.handle(
        _make_request(planner_output=payload, original_task="x" * 3000)
    )

    assert result.decomposition.subtasks[0].model_affinity == ""


# ---------------------------------------------------------------------------
# Passthrough: caller disabled
# ---------------------------------------------------------------------------


def test_handler_passthrough_caller_disabled() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request(decompose=False))

    assert result.status == EnumDecompositionStatus.PASSTHROUGH_CALLER_DISABLED
    assert len(result.decomposition.subtasks) == 1
    assert result.decomposition.subtasks[0].description == "x" * 3000


# ---------------------------------------------------------------------------
# Passthrough: below token threshold
# ---------------------------------------------------------------------------


def test_handler_passthrough_token_threshold() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(
        _make_request(original_task="Short task", token_threshold=2000)
    )

    assert result.status == EnumDecompositionStatus.PASSTHROUGH_TOKEN_THRESHOLD
    assert len(result.decomposition.subtasks) == 1
    assert result.decomposition.subtasks[0].description == "Short task"


def test_handler_no_passthrough_when_task_exceeds_threshold() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(
        _make_request(original_task="x" * 3000, token_threshold=2000)
    )

    assert result.status == EnumDecompositionStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Fallback on invalid JSON
# ---------------------------------------------------------------------------


def test_handler_fallback_on_invalid_json() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request(planner_output="not valid json %%"))

    assert result.status == EnumDecompositionStatus.FAILED_FALLBACK_PASSTHROUGH
    assert len(result.decomposition.subtasks) == 1


def test_handler_fallback_on_empty_subtasks() -> None:
    handler = HandlerSwarmDecomposer()
    result = handler.handle(_make_request(planner_output=json.dumps({"subtasks": []})))

    assert result.status == EnumDecompositionStatus.FAILED_FALLBACK_PASSTHROUGH


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_handler_cycle_detection_triggers_fallback() -> None:
    id_a = ModelSubtask.make_subtask_id("h", 0, "A")
    id_b = ModelSubtask.make_subtask_id("h", 1, "B")
    # Build a cycle: A depends on B which depends on A — inject via validate_decomposition
    st_a = ModelSubtask(
        subtask_id=id_a, description="A", model_affinity="", depends_on=(id_b,)
    )
    st_b = ModelSubtask(
        subtask_id=id_b, description="B", model_affinity="", depends_on=(id_a,)
    )
    _, _, rejection = validate_decomposition([st_a, st_b], [])

    assert rejection is not None
    assert "cycle" in rejection


# ---------------------------------------------------------------------------
# validate_decomposition unit tests
# ---------------------------------------------------------------------------


def _make_subtask_with(
    idx: int = 0,
    desc: str = "step",
    model_affinity: str = "",
    depends_on: tuple[str, ...] = (),
    estimated_tokens: int = 0,
) -> ModelSubtask:
    return ModelSubtask(
        subtask_id=ModelSubtask.make_subtask_id("testhash", idx, desc),
        description=desc,
        model_affinity=model_affinity,
        depends_on=depends_on,
        estimated_tokens=estimated_tokens,
    )


def test_validate_passes_clean_input() -> None:
    st0 = _make_subtask_with(0, "Step A")
    st1 = _make_subtask_with(1, "Step B", depends_on=(st0.subtask_id,))
    cleaned, warns, rejection = validate_decomposition([st0, st1], ["ep-1"])

    assert rejection is None
    assert len(cleaned) == 2
    assert warns == []


def test_validate_rejects_empty_description() -> None:
    st = _make_subtask_with(0, "   ")
    _, _, rejection = validate_decomposition([st], ["ep-1"])

    assert rejection is not None
    assert "empty description" in rejection


def test_validate_clears_unknown_affinity_with_warning() -> None:
    st = _make_subtask_with(0, "Step", model_affinity="ghost-ep")
    cleaned, warns, rejection = validate_decomposition([st], ["ep-1"])

    assert rejection is None
    assert cleaned[0].model_affinity == ""
    assert any("ghost-ep" in w for w in warns)


def test_validate_rejects_dangling_dependency() -> None:
    st = _make_subtask_with(0, "Step", depends_on=("nonexistent-id",))
    _, _, rejection = validate_decomposition([st], ["ep-1"])

    assert rejection is not None
    assert "dangling dependency" in rejection


def test_validate_context_window_warns_not_rejects() -> None:
    st = _make_subtask_with(0, "Big step", estimated_tokens=50000)
    cleaned, warns, rejection = validate_decomposition(
        [st], [], context_window_limit=8000
    )

    assert rejection is None
    assert any("context_window_limit" in w for w in warns)
    assert len(cleaned) == 1


# ---------------------------------------------------------------------------
# Input validation on ModelSwarmDecomposeRequest
# ---------------------------------------------------------------------------


def test_request_rejects_empty_planner_output() -> None:
    with pytest.raises(ValueError, match="planner_output"):
        ModelSwarmDecomposeRequest(
            planner_output="",
            planner_model_id="model-x",
            planner_output_hash="hash",
        )


def test_request_rejects_empty_planner_model_id() -> None:
    with pytest.raises(ValueError, match="planner_model_id"):
        ModelSwarmDecomposeRequest(
            planner_output="{}",
            planner_model_id="",
            planner_output_hash="hash",
        )


def test_request_rejects_empty_planner_output_hash() -> None:
    with pytest.raises(ValueError, match="planner_output_hash"):
        ModelSwarmDecomposeRequest(
            planner_output="{}",
            planner_model_id="model-x",
            planner_output_hash="",
        )


def test_request_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        ModelSwarmDecomposeRequest(  # type: ignore[call-arg]
            planner_output="{}",
            planner_model_id="model-x",
            planner_output_hash="hash",
            unexpected="boom",
        )
