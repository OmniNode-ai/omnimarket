"""Convergence Reducer — per-model F1 vs frontier tracking.

Pure function. Takes labeled findings (local_detected, frontier_detected) and
computes precision, recall, F1 overall and per-category.

No I/O. Deterministic.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
)


class ModelFindingLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID = Field(...)
    category: EnumFindingCategory = Field(...)
    local_detected: bool = Field(...)
    frontier_detected: bool = Field(...)


class ModelConvergenceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(...)
    labels: list[ModelFindingLabel] = Field(default_factory=list)


class ModelConvergenceOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(...)
    overall_f1: float = Field(default=0.0)
    overall_precision: float = Field(default=0.0)
    overall_recall: float = Field(default=0.0)
    by_category: dict[str, float] = Field(default_factory=dict)
    true_positives: int = Field(default=0)
    false_positives: int = Field(default=0)
    false_negatives: int = Field(default=0)
    total_labels: int = Field(default=0)


def _f1(tp: int, fp: int, fn: int) -> float:
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * (precision * recall) / (precision + recall)


def compute_convergence(input_data: ModelConvergenceInput) -> ModelConvergenceOutput:
    """Compute per-model F1 from labeled findings."""
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

    # Per-category
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


__all__: list[str] = [
    "ModelConvergenceInput",
    "ModelConvergenceOutput",
    "ModelFindingLabel",
    "compute_convergence",
]
