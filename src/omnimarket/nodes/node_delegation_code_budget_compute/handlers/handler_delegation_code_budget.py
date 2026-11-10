# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationCodeBudget: the call budget of a delegated code draft, from an overlay or sized from measured drafts (OMN-23434).

``resolve`` returns the overlay's budget for a task class and refuses a class the overlay does not name. ``size`` takes the
drafts measured for a class, keeps the ones that completed, and returns the slowest completed duration times the overlay's margin,
rounded up to the overlay's step; it refuses with too few completed samples and over the overlay's ceiling. A draft that timed out
is never a sample: its duration is the budget that cut it off, not what the draft needed. No clock, no I/O, no value of its own.
"""

from __future__ import annotations

import math

from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetRequest,
    ModelCodeBudgetResult,
)


class HandlerDelegationCodeBudget:
    def handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        if request.operation == "resolve":
            if request.task_class not in request.overlay.class_budgets_s:
                return ModelCodeBudgetResult(
                    operation="resolve",
                    task_class=request.task_class,
                    call_budget_s=None,
                    source="overlay",
                    refusal=EnumCodeBudgetRefusal.UNKNOWN_TASK_CLASS,
                    completed_samples=0,
                    detail=f"Unknown task class: {request.task_class}",
                )
            return ModelCodeBudgetResult(
                operation="resolve",
                task_class=request.task_class,
                call_budget_s=request.overlay.class_budgets_s[request.task_class],
                source="overlay",
                refusal=None,
                completed_samples=0,
                detail="",
            )

        completed = [m for m in request.measurements if m.completed]
        completed_samples = len(completed)
        if completed_samples < request.overlay.min_completed_samples:
            return ModelCodeBudgetResult(
                operation="size",
                task_class=request.task_class,
                call_budget_s=None,
                source="measured",
                refusal=EnumCodeBudgetRefusal.TOO_FEW_COMPLETED_SAMPLES,
                completed_samples=completed_samples,
                detail="",
            )

        raw = max(m.duration_s for m in completed) * request.overlay.sizing_margin
        step = request.overlay.round_up_to_s
        budget = math.ceil(raw / step) * step
        if budget > request.overlay.ceiling_s:
            return ModelCodeBudgetResult(
                operation="size",
                task_class=request.task_class,
                call_budget_s=None,
                source="measured",
                refusal=EnumCodeBudgetRefusal.OVER_CEILING,
                completed_samples=completed_samples,
                detail=f"Budget {budget} exceeds ceiling {request.overlay.ceiling_s}",
            )

        return ModelCodeBudgetResult(
            operation="size",
            task_class=request.task_class,
            call_budget_s=budget,
            source="measured",
            refusal=None,
            completed_samples=completed_samples,
            detail="",
        )
