# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_review_convergence_compute — per-model F1 vs frontier tracking.

Migrated to node_review_convergence_compute (OMN-13210 / B1); the convergence
eval tooling was re-homed from the deleted node_hostile_reviewer shell.
"""

from uuid import uuid4

from omnimarket.models.model_review_finding import (
    EnumFindingCategory,
)
from omnimarket.nodes.node_review_convergence_compute.models.model_review_convergence import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
    compute_convergence,
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
        ModelConvergenceInput(
            correlation_id=uuid4(), model_key="qwen3-coder", labels=labels
        )
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
        ModelConvergenceInput(
            correlation_id=uuid4(), model_key="qwen3-coder", labels=labels
        )
    )
    assert result.overall_f1 == 0.0


def test_empty_labels():
    result = compute_convergence(
        ModelConvergenceInput(
            correlation_id=uuid4(), model_key="qwen3-coder", labels=[]
        )
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
        ModelConvergenceInput(
            correlation_id=uuid4(), model_key="qwen3-coder", labels=labels
        )
    )
    assert result.by_category["security"] == 1.0
    assert result.by_category["logic_error"] == 0.0
