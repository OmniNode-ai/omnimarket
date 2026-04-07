"""Tests for the Convergence Reducer — per-model F1 vs frontier tracking."""

from uuid import uuid4

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_convergence_reducer import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
    compute_convergence,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
)


def test_perfect_agreement():
    labels = [
        ModelFindingLabel(
            finding_id=uuid4(),
            category=EnumFindingCategory.SECURITY,
            local_detected=True,
            frontier_detected=True,
        ),
        ModelFindingLabel(
            finding_id=uuid4(),
            category=EnumFindingCategory.LOGIC_ERROR,
            local_detected=True,
            frontier_detected=True,
        ),
    ]
    result = compute_convergence(
        ModelConvergenceInput(model_key="qwen3-coder", labels=labels)
    )
    assert isinstance(result, ModelConvergenceOutput)
    assert result.overall_f1 == 1.0


def test_all_false_positives():
    labels = [
        ModelFindingLabel(
            finding_id=uuid4(),
            category=EnumFindingCategory.SECURITY,
            local_detected=True,
            frontier_detected=False,
        ),
    ]
    result = compute_convergence(
        ModelConvergenceInput(model_key="qwen3-coder", labels=labels)
    )
    assert result.overall_f1 == 0.0


def test_empty_labels():
    result = compute_convergence(
        ModelConvergenceInput(model_key="qwen3-coder", labels=[])
    )
    assert result.overall_f1 == 0.0


def test_per_category_breakdown():
    labels = [
        ModelFindingLabel(
            finding_id=uuid4(),
            category=EnumFindingCategory.SECURITY,
            local_detected=True,
            frontier_detected=True,
        ),
        ModelFindingLabel(
            finding_id=uuid4(),
            category=EnumFindingCategory.SECURITY,
            local_detected=True,
            frontier_detected=True,
        ),
        ModelFindingLabel(
            finding_id=uuid4(),
            category=EnumFindingCategory.LOGIC_ERROR,
            local_detected=True,
            frontier_detected=False,
        ),
    ]
    result = compute_convergence(
        ModelConvergenceInput(model_key="qwen3-coder", labels=labels)
    )
    assert result.by_category["security"] == 1.0
    assert result.by_category["logic_error"] == 0.0
