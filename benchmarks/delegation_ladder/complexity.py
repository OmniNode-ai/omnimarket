# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scored task-complexity classifier (OMN-18300).

Reads every threshold, weight and band from
``contracts/task_complexity_rubric.v1.yaml``. This module holds no number of its
own, and ``tests/test_complexity.py`` asserts that by refusing any numeric
literal in the scoring path -- a threshold that drifts into code stops being
reviewable, which is the whole reason the rubric is a contract.

Six features are measured from the fed text. One, the count of dependent
reasoning steps, is declared per task under a counting rule stated in the
contract. The classification carries which is which, so a reader can see how
much of a score rests on a judgement.

Existing surfaces were read before this was written; the contract's header
records them and says what each does and does not do. The short version: the
workspace has contract-declared task CLASSES and one hardcoded feature-threshold
classifier, and nothing that derives a complexity rung from measured features
with reviewable thresholds.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONTRACT_PATH = (
    Path(__file__).resolve().parent / "contracts" / ("task_complexity_rubric.v1.yaml")
)

_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 _()./-]*:\s*$", re.MULTILINE)
_FENCE_OPEN_RE = re.compile(r"^```", re.MULTILINE)
_PYTHON_FENCE_RE = re.compile(r"^```(?:python|py)\b", re.MULTILINE)
_CATALOGUE_RE = re.compile(r"^CATALOGUE\b.*:\s*$", re.MULTILINE)


class ModelComplexityFeatures(BaseModel):
    """The feature vector behind one classification, with its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens_estimate: int
    distinct_sources: int
    output_kind: str
    verification: str
    requires_code_comprehension: bool
    selection_from_catalogue: bool
    execution_required: bool
    dependent_reasoning_steps: int
    measured: tuple[str, ...] = Field(
        description="Features read off the fed text. Not arguable."
    )
    declared: tuple[str, ...] = Field(
        description="Features supplied by the task under the contract's counting "
        "rule. Reproducible, but a judgement."
    )


class ModelComplexityClassification(BaseModel):
    """A derived rung, its score, and the points that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: str
    score: int
    points: dict[str, int]
    features: ModelComplexityFeatures
    contract_version: str


@lru_cache(maxsize=1)
def load_contract(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else CONTRACT_PATH
    with target.open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


def _band_points(value: float, bands: list[dict[str, Any]]) -> int:
    """First band whose ``below`` exceeds the value; a null ``below`` is the tail."""
    for band in bands:
        ceiling = band["below"]
        if ceiling is None or value < ceiling:
            return int(band["points"])
    raise ValueError("the contract's bands do not cover this value")


def measure(prompt: str, scorer: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Every feature the fed text answers on its own."""
    divisor = 4
    output_kind = contract["output_kind_by_scorer"][scorer]
    verification = contract["verification_by_scorer"][scorer]
    fences = len(_FENCE_OPEN_RE.findall(prompt))
    headers = len(_SECTION_HEADER_RE.findall(prompt))
    return {
        "input_tokens_estimate": math.ceil(len(prompt) / divisor),
        "distinct_sources": 1 + (fences // 2) + headers,
        "output_kind": output_kind,
        "verification": verification,
        "requires_code_comprehension": bool(_PYTHON_FENCE_RE.search(prompt)),
        "selection_from_catalogue": bool(_CATALOGUE_RE.search(prompt)),
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
    feature_spec = spec["features"]
    measured = measure(prompt, scorer, spec)

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

    features = ModelComplexityFeatures(
        **measured,
        dependent_reasoning_steps=dependent_reasoning_steps,
        measured=tuple(sorted(measured)),
        declared=("dependent_reasoning_steps",),
    )
    return ModelComplexityClassification(
        rung=rung,
        score=score,
        points=points,
        features=features,
        contract_version=str(spec["version"]),
    )
