# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure text measurement and rubric scoring shared with the benchmark."""

from __future__ import annotations

import math
import re
from typing import Any

from omnimarket.nodes.node_routing_complexity_compute.models.model_complexity import (
    ModelComplexityClassification,
    ModelComplexityFeatures,
)

_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 _()./-]*:\s*$", re.MULTILINE)
_FENCE_OPEN_RE = re.compile(r"^```", re.MULTILINE)
_PYTHON_FENCE_RE = re.compile(r"^```(?:python|py)\b", re.MULTILINE)
_CATALOGUE_RE = re.compile(r"^CATALOGUE\b.*:\s*$", re.MULTILINE)


def _band_points(value: float, bands: list[dict[str, Any]]) -> int:
    """First band whose ``below`` exceeds the value; a null ``below`` is the tail."""
    for band in bands:
        ceiling = band["below"]
        if ceiling is None or value < ceiling:
            return int(band["points"])
    raise ValueError("the contract's bands do not cover this value")


def measure_text(prompt: str) -> dict[str, Any]:
    """Only features mechanically measured from the fed text."""
    divisor = 4
    fences = len(_FENCE_OPEN_RE.findall(prompt))
    headers = len(_SECTION_HEADER_RE.findall(prompt))
    return {
        "input_tokens_estimate": math.ceil(len(prompt) / divisor),
        "distinct_sources": 1 + (fences // 2) + headers,
        "requires_code_comprehension": bool(_PYTHON_FENCE_RE.search(prompt)),
        "selection_from_catalogue": bool(_CATALOGUE_RE.search(prompt)),
    }


def score_features(
    features: ModelComplexityFeatures, spec: dict[str, Any]
) -> ModelComplexityClassification:
    """Score an already resolved feature vector using only rubric weights."""
    feature_spec = spec["features"]
    measured = features.model_dump()
    dependent_reasoning_steps = features.dependent_reasoning_steps

    points: dict[str, int] = {
        "input_tokens_estimate": _band_points(
            measured["input_tokens_estimate"],
            feature_spec["input_tokens_estimate"]["bands"],
        ),
        "distinct_sources": _band_points(
            measured["distinct_sources"], feature_spec["distinct_sources"]["bands"]
        ),
        "output_kind": int(
            feature_spec["output_kind"]["points"][measured["output_kind"]]
        ),
        "requires_code_comprehension": (
            int(feature_spec["requires_code_comprehension"]["points_when_true"])
            if measured["requires_code_comprehension"]
            else 0
        ),
        "selection_from_catalogue": (
            int(feature_spec["selection_from_catalogue"]["points_when_true"])
            if measured["selection_from_catalogue"]
            else 0
        ),
        "execution_required": (
            int(feature_spec["execution_required"]["points_when_true"])
            if measured["execution_required"]
            else 0
        ),
        "dependent_reasoning_steps": min(
            max(dependent_reasoning_steps - 1, 0)
            * int(
                feature_spec["dependent_reasoning_steps"]["points_per_step_above_one"]
            ),
            int(feature_spec["dependent_reasoning_steps"]["max_points"]),
        ),
    }
    score = sum(points.values())

    rung = next(
        entry["id"] for entry in spec["rungs"] if score <= int(entry["max_score"])
    )

    return ModelComplexityClassification(
        rung=rung,
        score=score,
        points=points,
        features=features,
        contract_version=str(spec["version"]),
    )
