# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Scorer for the ADK evaluation spike (P9).

Compares a ModelTypeDebtReport against the P5 labeled_sample.yaml and
emits precision/recall/F1 in both binary (actionable vs not) and 4-class
(critical/major/minor/noise) modes.

Sample is LLM-self-labeled (not human-gold); scores are directional.
"""

from omnimarket.experiments.adk_eval.scorer.scorer import (
    ModelConfusionMatrix,
    ModelPerClassScore,
    ModelTrackScore,
    load_labels,
    score_report,
    score_reports,
)

__all__ = [
    "ModelConfusionMatrix",
    "ModelPerClassScore",
    "ModelTrackScore",
    "load_labels",
    "score_report",
    "score_reports",
]
