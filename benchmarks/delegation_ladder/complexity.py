# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility API for the delegation ladder's benchmark classifier.

Routing uses the packaged node_routing_complexity_compute; only this benchmark
wrapper derives features from scorers or accepts caller-declared step counts.
The scoring implementation and rubric are shared with the packaged compute.
"""

from __future__ import annotations

from typing import Any

from omnimarket.inference.task_class_authority import (
    TASK_COMPLEXITY_RUBRIC_PATH as CONTRACT_PATH,
)
from omnimarket.inference.task_class_authority import (
    load_task_complexity_rubric as load_contract,
)
from omnimarket.nodes.node_routing_complexity_compute.handlers.handler_complexity_scoring import (
    measure_text,
    score_features,
)
from omnimarket.nodes.node_routing_complexity_compute.models.model_complexity import (
    ModelComplexityClassification,
    ModelComplexityFeatures,
)

__all__ = [
    "CONTRACT_PATH",
    "ModelComplexityClassification",
    "ModelComplexityFeatures",
    "classify",
    "load_contract",
    "measure",
    "measure_text",
    "score_features",
]


def measure(prompt: str, scorer: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Benchmark-only measurement, including its scorer-derived features."""
    output_kind = contract["output_kind_by_scorer"][scorer]
    verification = contract["verification_by_scorer"][scorer]
    return {
        **measure_text(prompt),
        "output_kind": output_kind,
        "verification": verification,
        "execution_required": bool(contract["execution_by_verification"][verification]),
    }


def classify(
    prompt: str,
    scorer: str,
    dependent_reasoning_steps: int,
    contract: dict[str, Any] | None = None,
) -> ModelComplexityClassification:
    """Derive a rung from the bundle, under the contract's thresholds."""
    spec = contract if contract is not None else load_contract()
    measured = measure(prompt, scorer, spec)
    features = ModelComplexityFeatures(
        **measured,
        dependent_reasoning_steps=dependent_reasoning_steps,
        measured=tuple(sorted(measured)),
        declared=("dependent_reasoning_steps",),
    )
    return score_features(features, spec)
