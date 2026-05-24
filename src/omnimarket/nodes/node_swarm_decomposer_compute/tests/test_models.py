# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import uuid

import pytest

from omnimarket.nodes.node_swarm_decomposer_compute.models.enums import (
    EnumDecompositionStatus,
    EnumSubtaskCategory,
    EnumSubtaskStatus,
    EnumSwarmCapability,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_decomposition import (
    ModelDecomposition,
)
from omnimarket.nodes.node_swarm_decomposer_compute.models.model_subtask import (
    ModelSubtask,
)


def _make_subtask(idx: int = 0, desc: str = "step") -> ModelSubtask:
    return ModelSubtask(
        subtask_id=ModelSubtask.make_subtask_id("testhash", idx, desc),
        description=desc,
        model_affinity="",
    )


def _minimal_decomposition(**overrides: object) -> ModelDecomposition:
    st = _make_subtask()
    defaults: dict[str, object] = {
        "original_task": "Do something",
        "original_task_hash": "testhash",
        "subtasks": (st,),
        "decomposition_model": "model-x",
        "decomposition_endpoint_id": "ep-1",
        "decomposition_latency_ms": 50,
        "decomposition_status": EnumDecompositionStatus.SUCCEEDED,
        "decomposition_run_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
    }
    defaults.update(overrides)
    return ModelDecomposition(**defaults)


# ---------------------------------------------------------------------------
# Enum exhaustive checks
# ---------------------------------------------------------------------------


def test_enum_swarm_capability_all_members() -> None:
    expected = {
        "code_generation",
        "structured_output",
        "refactoring",
        "reasoning",
        "analysis",
        "math",
        "planning",
        "synthesis",
        "general",
    }
    assert {m.value for m in EnumSwarmCapability} == expected


def test_enum_subtask_category_all_members() -> None:
    expected = {"code", "reasoning", "synthesis", "analysis", "planning", "general"}
    assert {m.value for m in EnumSubtaskCategory} == expected


def test_enum_decomposition_status_all_members() -> None:
    expected = {
        "succeeded",
        "failed_fallback_passthrough",
        "passthrough_token_threshold",
        "passthrough_caller_disabled",
    }
    assert {m.value for m in EnumDecompositionStatus} == expected


def test_enum_subtask_status_all_members() -> None:
    expected = {
        "succeeded",
        "failed",
        "timeout",
        "skipped_dependency_failed",
        "context_window_exceeded",
    }
    assert {m.value for m in EnumSubtaskStatus} == expected


# ---------------------------------------------------------------------------
# ModelSubtask
# ---------------------------------------------------------------------------


def test_model_subtask_defaults() -> None:
    st = ModelSubtask(
        subtask_id="abc123", description="Do something", model_affinity="ep-1"
    )
    assert st.depends_on == ()
    assert st.estimated_tokens == 0
    assert st.token_estimation_method == "char_ratio"
    assert st.category == EnumSubtaskCategory.GENERAL


def test_model_subtask_frozen() -> None:
    st = ModelSubtask(
        subtask_id="abc123", description="Do something", model_affinity=""
    )
    with pytest.raises(ValueError, match="frozen"):
        st.subtask_id = "other"  # type: ignore[misc]


def test_model_subtask_extra_forbidden() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        ModelSubtask(  # type: ignore[call-arg]
            subtask_id="x",
            description="y",
            model_affinity="",
            unexpected_field="boom",
        )


def test_make_subtask_id_deterministic() -> None:
    id1 = ModelSubtask.make_subtask_id("hash_a", 0, "description")
    id2 = ModelSubtask.make_subtask_id("hash_a", 0, "description")
    assert id1 == id2
    assert len(id1) == 16


def test_make_subtask_id_varies_by_index() -> None:
    id1 = ModelSubtask.make_subtask_id("hash_a", 0, "description")
    id2 = ModelSubtask.make_subtask_id("hash_a", 1, "description")
    assert id1 != id2


def test_make_subtask_id_varies_by_task_hash() -> None:
    id1 = ModelSubtask.make_subtask_id("hash_a", 0, "description")
    id2 = ModelSubtask.make_subtask_id("hash_b", 0, "description")
    assert id1 != id2


# ---------------------------------------------------------------------------
# ModelDecomposition
# ---------------------------------------------------------------------------


def test_model_decomposition_frozen() -> None:
    d = _minimal_decomposition()
    with pytest.raises(ValueError, match="frozen"):
        d.original_task = "other"  # type: ignore[misc]


def test_model_decomposition_extra_forbidden() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        _minimal_decomposition(not_a_field="boom")


def test_model_decomposition_warnings_default_empty() -> None:
    d = _minimal_decomposition()
    assert d.warnings == ()
    assert isinstance(d.warnings, tuple)


def test_model_decomposition_warnings_stored() -> None:
    d = _minimal_decomposition(warnings=("w1", "w2"))
    assert d.warnings == ("w1", "w2")


def test_model_decomposition_subtasks_is_tuple() -> None:
    d = _minimal_decomposition()
    assert isinstance(d.subtasks, tuple)


def test_model_decomposition_correlation_id_default_empty() -> None:
    st = _make_subtask()
    d = ModelDecomposition(
        original_task="task",
        original_task_hash="hash",
        subtasks=(st,),
        decomposition_model="m",
        decomposition_endpoint_id="ep",
        decomposition_latency_ms=0,
        decomposition_status=EnumDecompositionStatus.SUCCEEDED,
        decomposition_run_id="run1",
    )
    assert d.correlation_id == ""
