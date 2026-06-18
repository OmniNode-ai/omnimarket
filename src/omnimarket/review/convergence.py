# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Convergence computation — per-model precision / recall / F1 vs frontier labels.

Shared eval-time tooling re-homed from the deleted node_hostile_reviewer shell
(OMN-13210 / B1). Pure function. No I/O. Deterministic.
"""

from __future__ import annotations

from omnimarket.review.node_io import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
)


def _f1(tp: int, fp: int, fn: int) -> float:
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * (precision * recall) / (precision + recall)


def compute_convergence(input_data: ModelConvergenceInput) -> ModelConvergenceOutput:
    """Compute per-model precision / recall / F1 from labeled findings."""
    if not input_data.labels:
        return ModelConvergenceOutput(model_key=input_data.model_key)

    tp = sum(
        1 for lb in input_data.labels if lb.local_detected and lb.frontier_detected
    )
    fp = sum(
        1 for lb in input_data.labels if lb.local_detected and not lb.frontier_detected
    )
    fn = sum(
        1 for lb in input_data.labels if not lb.local_detected and lb.frontier_detected
    )

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    overall = _f1(tp, fp, fn)

    categories: dict[str, list[ModelFindingLabel]] = {}
    for label in input_data.labels:
        categories.setdefault(label.category.value, []).append(label)

    by_category: dict[str, float] = {}
    for cat, labels in categories.items():
        cat_tp = sum(1 for lb in labels if lb.local_detected and lb.frontier_detected)
        cat_fp = sum(
            1 for lb in labels if lb.local_detected and not lb.frontier_detected
        )
        cat_fn = sum(
            1 for lb in labels if not lb.local_detected and lb.frontier_detected
        )
        by_category[cat] = _f1(cat_tp, cat_fp, cat_fn)

    return ModelConvergenceOutput(
        model_key=input_data.model_key,
        overall_f1=overall,
        overall_precision=precision,
        overall_recall=recall,
        by_category=by_category,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        total_labels=len(input_data.labels),
    )


__all__: list[str] = ["compute_convergence"]
