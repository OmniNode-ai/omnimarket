# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grounding budgets and measured correct ceilings per backend and task class.

OMN-20486 declared per-task-class grounding budgets. OMN-22220 adds the
measured correct ceiling: the largest input a backend was measured to answer a
class correctly. Both are read from ``configs/grounding_budget_by_task_class.yaml``
and shared by the routing reducer, the bus workflow and the local dispatch port.

A rung is not offered a task above its ceiling, and an answer from above the
ceiling that the class scorer did not pass is refused by the gate.
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache

import yaml

from omnimarket.models.delegation.model_grounding_budget_by_task_class import (
    ModelGroundingBudgetByTaskClass,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult

# OMN-18297: the divisor of the character-based input measurement. It is the
# repo's existing estimator (``estimate_tokens``, node_context_artifact_resolver
# _compute) and it UNDER-counts: the recorded 57,120-character prompt of
# delegation d715f096-27b9-444f-9355-3554819ef8a5 measures 14,280 here against
# the provider's metered 18,245, about 22% low. The budget is declared in these
# same units so the comparison is like-for-like, and both numbers are recorded
# on the receipt so a reader never has to guess which one a budget refers to.
_GROUNDING_TOKEN_CHARS_PER_TOKEN: int = 4


def measure_grounding_input_tokens(text: str) -> int:
    """Measure a prompt in the units the grounding budget is declared in."""
    if not text:
        return 0
    return max(1, -(-len(text) // _GROUNDING_TOKEN_CHARS_PER_TOKEN))


@lru_cache(maxsize=1)
def load_grounding_budget_by_task_class() -> ModelGroundingBudgetByTaskClass:
    """Load the packaged per-task-class budgets and ceilings (OMN-20486, OMN-22220)."""
    resource = importlib.resources.files("omnimarket").joinpath(
        "configs/grounding_budget_by_task_class.yaml"
    )
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    return ModelGroundingBudgetByTaskClass.model_validate(data)


def exceeds_measured_correct_ceiling(
    backend_ref: str | None, task_type: str, input_tokens: int
) -> bool:
    """Whether ``input_tokens`` is above the backend's measured ceiling for the class.

    ``False`` when no ceiling is declared: an unmeasured pair is not refused here.
    """
    if backend_ref is None:
        return False
    ceiling = load_grounding_budget_by_task_class().ceiling_for(backend_ref, task_type)
    return ceiling is not None and input_tokens > ceiling


def refuse_above_measured_correct_ceiling(
    result: ModelQualityGateResult,
    verdict: ModelAttemptRubricVerdict,
    *,
    backend_ref: str | None,
    task_class: str,
    request_text: str,
) -> ModelQualityGateResult:
    """Refuse an above-ceiling answer the class scorer did not pass.

    Above the measured correct ceiling the gate's floor is not evidence of a
    correct answer (H6-05: gate 0.733, scorer 0.0), so only a scorer PASS keeps
    it. An existing refusal, a scorer PASS, a call at or below the ceiling and a
    backend with no declared ceiling are returned unchanged.
    """
    if not result.passed or verdict.outcome == "PASS":
        return result
    input_tokens = measure_grounding_input_tokens(request_text)
    if not exceeds_measured_correct_ceiling(backend_ref, task_class, input_tokens):
        return result
    ceiling = load_grounding_budget_by_task_class().ceiling_for(
        backend_ref or "", task_class
    )
    criteria = verdict.failed_criteria + verdict.undetermined_criteria
    return result.model_copy(
        update={
            "passed": False,
            "fail_category": "rubric_failed",
            "failure_reasons": (
                f"above_measured_correct_ceiling: input {input_tokens} tokens > "
                f"{backend_ref} {task_class} ceiling {ceiling}; scorer "
                f"{verdict.outcome}",
                *(f"rubric_failed: {criterion}" for criterion in criteria),
            ),
            "fallback_recommended": True,
        }
    )


__all__ = [
    "exceeds_measured_correct_ceiling",
    "load_grounding_budget_by_task_class",
    "measure_grounding_input_tokens",
    "refuse_above_measured_correct_ceiling",
]
