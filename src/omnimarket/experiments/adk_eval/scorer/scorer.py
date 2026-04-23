# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Scorer implementation for the ADK evaluation spike (P9).

Joins a ModelTypeDebtReport against the P5 labeled sample by `file:line`
key, collapses duplicate labels at the same position to the most-severe
tier, and computes:

- Binary precision/recall/F1, where critical+major → 1 (actionable) and
  minor+noise → 0 (not actionable).
- 4-class per-class precision/recall/F1 + macro-averaged F1.
- A confusion-matrix-style correctness summary.

The input labeled sample is LLM-self-labeled, not human-gold; all scores
are directional and surfaced with that caveat in `score_reports`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from omnibase_core.enums.enum_type_debt_priority import EnumTypeDebtPriority
from omnibase_core.models.quality.model_type_debt_report import ModelTypeDebtReport
from pydantic import BaseModel, ConfigDict, Field

_SEVERITY_ORDER: dict[EnumTypeDebtPriority, int] = {
    EnumTypeDebtPriority.CRITICAL: 3,
    EnumTypeDebtPriority.MAJOR: 2,
    EnumTypeDebtPriority.MINOR: 1,
    EnumTypeDebtPriority.NOISE: 0,
}

_ACTIONABLE = frozenset({EnumTypeDebtPriority.CRITICAL, EnumTypeDebtPriority.MAJOR})


class ModelPerClassScore(BaseModel):
    """Precision / recall / F1 for a single priority tier."""

    precision: float = Field(..., ge=0.0, le=1.0)
    recall: float = Field(..., ge=0.0, le=1.0)
    f1: float = Field(..., ge=0.0, le=1.0)
    tp: int = Field(..., ge=0)
    fp: int = Field(..., ge=0)
    fn: int = Field(..., ge=0)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelConfusionMatrix(BaseModel):
    """Matched-finding summary (not a full NxN matrix — just totals)."""

    total_matched: int = Field(..., ge=0)
    total_correct: int = Field(..., ge=0)
    # 4-class confusion counts: {true_label: {pred_label: count}}
    cells: dict[str, dict[str, int]] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelTrackScore(BaseModel):
    """Full scoring result for one track's report."""

    binary_precision: float = Field(..., ge=0.0, le=1.0)
    binary_recall: float = Field(..., ge=0.0, le=1.0)
    binary_f1: float = Field(..., ge=0.0, le=1.0)
    macro_f1: float = Field(..., ge=0.0, le=1.0)
    per_class: dict[str, ModelPerClassScore]
    confusion_matrix: ModelConfusionMatrix
    matched_findings: int = Field(..., ge=0)
    unmatched_findings: int = Field(..., ge=0)
    missing_labels: int = Field(..., ge=0)
    total_labels: int = Field(..., ge=0)

    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_json_dict(self) -> dict[str, Any]:
        """Flatten for scores.json emission."""
        return {
            "binary_precision": round(self.binary_precision, 6),
            "binary_recall": round(self.binary_recall, 6),
            "binary_f1": round(self.binary_f1, 6),
            "macro_f1": round(self.macro_f1, 6),
            "per_class": {
                tier: {
                    "precision": round(score.precision, 6),
                    "recall": round(score.recall, 6),
                    "f1": round(score.f1, 6),
                    "tp": score.tp,
                    "fp": score.fp,
                    "fn": score.fn,
                }
                for tier, score in self.per_class.items()
            },
            "confusion_matrix": {
                "total_matched": self.confusion_matrix.total_matched,
                "total_correct": self.confusion_matrix.total_correct,
                "cells": self.confusion_matrix.cells,
            },
            "matched_findings": self.matched_findings,
            "unmatched_findings": self.unmatched_findings,
            "missing_labels": self.missing_labels,
            "total_labels": self.total_labels,
        }


def _normalize_ref(raw: str) -> str:
    """Strip a leading '<repo>:' prefix so labels and reports join cleanly.

    P5 labels use '<repo>:<file>:<line>'; track prompts instruct LLMs to
    emit '<file>:<line>'. If there are 3+ colon-separated parts, drop the
    first (the repo). If only 2, assume it's already normalized.
    """
    parts = raw.split(":", 1)
    if len(parts) != 2:
        return raw
    _, rest = parts
    # If `rest` still contains a colon (file:line), the first part is a repo prefix.
    if ":" in rest:
        return rest
    return raw


def load_labels(path: Path) -> dict[str, EnumTypeDebtPriority]:
    """Load labeled_sample.yaml → {normalized_finding_ref: most_severe_priority}."""
    doc = yaml.safe_load(path.read_text())
    raw_labels = doc.get("labels", [])
    collapsed: dict[str, EnumTypeDebtPriority] = {}
    for entry in raw_labels:
        ref = _normalize_ref(str(entry["finding_ref"]))
        priority = EnumTypeDebtPriority(entry["priority"])
        existing = collapsed.get(ref)
        if existing is None or _SEVERITY_ORDER[priority] > _SEVERITY_ORDER[existing]:
            collapsed[ref] = priority
    return collapsed


def _binary(priority: EnumTypeDebtPriority) -> int:
    return 1 if priority in _ACTIONABLE else 0


def _prf1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Standard precision/recall/F1 with the usual zero-division convention."""
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return precision, recall, f1


def score_report(
    report: ModelTypeDebtReport,
    labels: dict[str, EnumTypeDebtPriority],
) -> ModelTrackScore:
    """Score one track's ModelTypeDebtReport against loaded labels."""
    y_true: list[EnumTypeDebtPriority] = []
    y_pred: list[EnumTypeDebtPriority] = []
    matched_refs: set[str] = set()
    unmatched = 0

    for prio in report.findings_prioritized:
        ref = _normalize_ref(prio.finding_ref)
        label = labels.get(ref)
        if label is None:
            unmatched += 1
            continue
        matched_refs.add(ref)
        y_true.append(label)
        y_pred.append(prio.priority)

    missing_labels = len(set(labels.keys()) - matched_refs)

    # Binary metrics
    y_true_bin = [_binary(p) for p in y_true]
    y_pred_bin = [_binary(p) for p in y_pred]
    tp_b = sum(
        1 for t, p in zip(y_true_bin, y_pred_bin, strict=True) if t == 1 and p == 1
    )
    fp_b = sum(
        1 for t, p in zip(y_true_bin, y_pred_bin, strict=True) if t == 0 and p == 1
    )
    fn_b = sum(
        1 for t, p in zip(y_true_bin, y_pred_bin, strict=True) if t == 1 and p == 0
    )
    bin_p, bin_r, bin_f1 = _prf1(tp_b, fp_b, fn_b)

    # 4-class per-tier metrics
    per_class: dict[str, ModelPerClassScore] = {}
    f1_sum = 0.0
    for tier in EnumTypeDebtPriority:
        tp = sum(
            1 for t, p in zip(y_true, y_pred, strict=True) if p == tier and t == tier
        )
        fp = sum(
            1 for t, p in zip(y_true, y_pred, strict=True) if p == tier and t != tier
        )
        fn = sum(
            1 for t, p in zip(y_true, y_pred, strict=True) if t == tier and p != tier
        )
        precision, recall, f1 = _prf1(tp, fp, fn)
        per_class[tier.value] = ModelPerClassScore(
            precision=precision,
            recall=recall,
            f1=f1,
            tp=tp,
            fp=fp,
            fn=fn,
        )
        f1_sum += f1
    macro_f1 = f1_sum / len(EnumTypeDebtPriority)

    # Confusion cells
    cells: dict[str, dict[str, int]] = {
        t.value: {p.value: 0 for p in EnumTypeDebtPriority}
        for t in EnumTypeDebtPriority
    }
    for t, p in zip(y_true, y_pred, strict=True):
        cells[t.value][p.value] += 1
    total_correct = sum(cells[t][t] for t in cells)

    matrix = ModelConfusionMatrix(
        total_matched=len(y_true),
        total_correct=total_correct,
        cells=cells,
    )

    return ModelTrackScore(
        binary_precision=bin_p,
        binary_recall=bin_r,
        binary_f1=bin_f1,
        macro_f1=macro_f1,
        per_class=per_class,
        confusion_matrix=matrix,
        matched_findings=len(y_true),
        unmatched_findings=unmatched,
        missing_labels=missing_labels,
        total_labels=len(labels),
    )


def _zero_track(total_labels: int) -> dict[str, Any]:
    zero_per_class = {
        tier.value: {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
        }
        for tier in EnumTypeDebtPriority
    }
    zero_cells = {
        t.value: {p.value: 0 for p in EnumTypeDebtPriority}
        for t in EnumTypeDebtPriority
    }
    return {
        "binary_precision": 0.0,
        "binary_recall": 0.0,
        "binary_f1": 0.0,
        "macro_f1": 0.0,
        "per_class": zero_per_class,
        "confusion_matrix": {
            "total_matched": 0,
            "total_correct": 0,
            "cells": zero_cells,
        },
        "matched_findings": 0,
        "unmatched_findings": 0,
        "missing_labels": total_labels,
        "total_labels": total_labels,
    }


def score_reports(
    track_a: ModelTypeDebtReport | None,
    track_b: ModelTypeDebtReport | None,
    labels: dict[str, EnumTypeDebtPriority],
    extra_caveats: list[str] | None = None,
) -> dict[str, Any]:
    """Score both tracks; fill any missing track with zeros + a caveat."""
    caveats: list[str] = [
        "LLM-self-labeled sample (P5), not human-gold.",
        "N=30 findings; directional only, not a statistically rigorous claim.",
        "Duplicate labels at the same file:line collapsed to most-severe tier.",
    ]
    if extra_caveats:
        caveats.extend(extra_caveats)

    total_labels = len(labels)
    if track_a is None:
        track_a_out = _zero_track(total_labels)
        caveats.append("track_a report missing — scored as zeros.")
    else:
        track_a_out = score_report(track_a, labels).to_json_dict()

    if track_b is None:
        track_b_out = _zero_track(total_labels)
        caveats.append("track_b report missing — scored as zeros.")
    else:
        track_b_out = score_report(track_b, labels).to_json_dict()

    return {
        "track_a": track_a_out,
        "track_b": track_b_out,
        "caveats": caveats,
    }


__all__ = [
    "ModelConfusionMatrix",
    "ModelPerClassScore",
    "ModelTrackScore",
    "load_labels",
    "score_report",
    "score_reports",
]
