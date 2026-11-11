# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grounding budgets and measured correct ceilings per backend and task class.

OMN-20486 declared per-task-class grounding budgets, read from
``configs/grounding_budget_by_task_class.yaml``. OMN-22220 added the measured
correct ceiling: the largest input a backend was measured to answer a class
correctly. Since OMN-23631 the ceiling is an overlay value, the key
``delegation.code_budget.input_ceilings.<backend>.<task_class>`` of
``node_delegation_code_budget_compute``, read through that node's
``input_ceiling`` operation; there is no packaged value and no fallback. A
install with no overlay root naming the code budget overlay (a customer's clean install) declares no ceilings, so
none applies; an overlay that is present but unreadable or invalid raises :class:`NodeOverlayError`, never a default.
The overlay is read on every call, so a changed ceiling is in force on the next routing.

A rung is not offered a task above its ceiling, and an answer from above the
ceiling that the class scorer did not pass is refused by the gate.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Mapping
from functools import lru_cache

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.delegation.model_grounding_budget_by_task_class import (
    ModelGroundingBudgetByTaskClass,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.models.model_delegation_code_budget import (
    INPUT_CEILING_OVERLAY_KEY,
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
    input_ceiling_overlay_key,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

# OMN-18297: the divisor of the character-based input measurement. It is the
# repo's existing estimator (``estimate_tokens``, node_context_artifact_resolver
# _compute) and it UNDER-counts: the recorded 57,120-character prompt of
# delegation d715f096-27b9-444f-9355-3554819ef8a5 measures 14,280 here against
# the provider's metered 18,245, about 22% low. The budget is declared in these
# same units so the comparison is like-for-like, and both numbers are recorded
# on the receipt so a reader never has to guess which one a budget refers to.
_GROUNDING_TOKEN_CHARS_PER_TOKEN: int = 4

#: The node whose overlay carries the input ceilings (OMN-23631).
CODE_BUDGET_OVERLAY_ID = "node_delegation_code_budget_compute"


class InputLimitNotDeclaredError(NodeOverlayError):
    """Raised when a caller requires an input limit the overlay does not declare."""


def measure_grounding_input_tokens(text: str) -> int:
    """Measure a prompt in the units the grounding budget is declared in."""
    if not text:
        return 0
    return max(1, -(-len(text) // _GROUNDING_TOKEN_CHARS_PER_TOKEN))


@lru_cache(maxsize=1)
def load_grounding_budget_by_task_class() -> ModelGroundingBudgetByTaskClass:
    """Load the packaged per-task-class budgets (OMN-20486)."""
    resource = importlib.resources.files("omnimarket").joinpath(
        "configs/grounding_budget_by_task_class.yaml"
    )
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    return ModelGroundingBudgetByTaskClass.model_validate(data)


def measured_correct_ceiling(
    backend_ref: str, task_type: str, environ: Mapping[str, str] | None = None
) -> int | None:
    """The overlay's input ceiling for a backend and class, or ``None`` when it names none.

    Read through ``node_delegation_code_budget_compute``'s ``input_ceiling``
    operation. A pair the overlay does not name has no ceiling (it was never
    measured), and an install no overlay root names the overlay on declares no
    ceilings at all. An overlay that is present but unreadable or invalid raises
    :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a default. A backend that is not named
    (an empty reference) has no ceiling.
    """
    if not backend_ref:
        return None
    try:
        overlay = load_structured_overlay(
            CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None
    result = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="input_ceiling",
            task_class=task_type,
            backend=backend_ref,
            overlay=overlay,
        )
    )
    if result.refusal is EnumCodeBudgetRefusal.UNNAMED_INPUT_CEILING:
        return None
    return result.input_ceiling_tokens


def local_input_window(
    backend_id: str, environ: Mapping[str, str] | None = None
) -> int:
    """The largest local input the overlay declares for ``backend_id`` in any class.

    Read from ``delegation.code_budget.input_ceilings.<backend>`` of the code
    budget overlay (OMN-23631). An install no overlay root names the overlay on,
    or a backend the overlay does not name, raises
    :class:`InputLimitNotDeclaredError`; an overlay that is present but unreadable
    or invalid still raises :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`,
    never a default.
    """
    try:
        overlay = load_structured_overlay(
            CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, environ
        )
    except NodeOverlayNotFoundError:
        raise InputLimitNotDeclaredError(
            f"{CODE_BUDGET_OVERLAY_ID}: no overlay root names the code budget "
            "overlay, so no local input limit is declared"
        ) from None
    if backend_id not in overlay.input_ceilings:
        raise InputLimitNotDeclaredError(
            f"{INPUT_CEILING_OVERLAY_KEY}.{backend_id}: name it in the code "
            "budget overlay"
        )
    return max(overlay.input_ceilings[backend_id].values())


def required_input_ceiling(
    backend_id: str, task_type: str, environ: Mapping[str, str] | None = None
) -> int:
    """The overlay's input ceiling for a backend and class, which must be declared.

    The required form of :func:`measured_correct_ceiling`: a pair the overlay
    does not name raises :class:`InputLimitNotDeclaredError` instead of
    returning ``None``, because the local input limit has no packaged value to
    fall back to.
    """
    ceiling = measured_correct_ceiling(backend_id, task_type, environ)
    if ceiling is None:
        raise InputLimitNotDeclaredError(
            f"{input_ceiling_overlay_key(backend_id, task_type)}: the local input "
            "limit has no packaged value"
        )
    return ceiling


def grounded_input_budget(backend_id: str, task_type: str) -> int | None:
    """The class budget for ``backend_id``, bounded by its measured correct ceiling (OMN-22220).

    ``None`` when no budget is declared and no ceiling is named.
    """
    declared = load_grounding_budget_by_task_class().budget_for(backend_id, task_type)
    ceiling = measured_correct_ceiling(backend_id, task_type)
    if ceiling is None:
        return declared
    return ceiling if declared is None else min(declared, ceiling)


def exceeds_measured_correct_ceiling(
    backend_ref: str | None, task_type: str, input_tokens: int
) -> bool:
    """Whether ``input_tokens`` is above the backend's measured ceiling for the class.

    ``False`` when the overlay names no ceiling for the pair: an unmeasured pair is not refused here.
    """
    if backend_ref is None:
        return False
    ceiling = measured_correct_ceiling(backend_ref, task_type)
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
    ceiling = measured_correct_ceiling(backend_ref or "", task_class)
    key = input_ceiling_overlay_key(backend_ref or "", task_class)
    criteria = verdict.failed_criteria + verdict.undetermined_criteria
    return result.model_copy(
        update={
            "passed": False,
            "fail_category": "rubric_failed",
            "failure_reasons": (
                f"above_measured_correct_ceiling: input {input_tokens} tokens > "
                f"{backend_ref} {task_class} ceiling {ceiling} (overlay key {key}); "
                f"scorer {verdict.outcome}",
                *(f"rubric_failed: {criterion}" for criterion in criteria),
            ),
            "fallback_recommended": True,
        }
    )


__all__ = [
    "CODE_BUDGET_OVERLAY_ID",
    "InputLimitNotDeclaredError",
    "exceeds_measured_correct_ceiling",
    "grounded_input_budget",
    "load_grounding_budget_by_task_class",
    "local_input_window",
    "measure_grounding_input_tokens",
    "measured_correct_ceiling",
    "refuse_above_measured_correct_ceiling",
    "required_input_ceiling",
]
