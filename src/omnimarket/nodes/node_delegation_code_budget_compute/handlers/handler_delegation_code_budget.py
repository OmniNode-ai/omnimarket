# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationCodeBudget: the call budget of a delegated code draft, from an overlay or sized from measured drafts (OMN-23434).

``resolve`` returns the overlay's budget for a task class and refuses a class the overlay does not name. ``size`` takes the
drafts measured for a class, keeps the ones that completed, and returns the slowest completed duration times the overlay's margin,
rounded up to the overlay's step; it refuses with too few completed samples and over the overlay's ceiling. A draft that timed out
is never a sample: its duration is the budget that cut it off, not what the draft needed. ``input_ceiling`` returns the overlay's input
ceiling for a backend and task class and refuses a pair the overlay does not name (OMN-23631). ``output_budget`` returns a backend's
output budget for one request: the overlay's window, less the request's measured input tokens, less the overlay's margin, held to the
overlay's ceiling; it refuses a backend the overlay does not name and a request whose room is under the overlay's floor (OMN-26640).
No clock, no I/O, no value of its own.
"""

from __future__ import annotations

import math

from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetRequest,
    ModelCodeBudgetResult,
    ModelOutputBudget,
    input_ceiling_overlay_key,
)


class HandlerDelegationCodeBudget:
    def handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        if request.operation == "sampling":
            sampling = (
                request.overlay.class_sampling.get(request.task_class)
                if request.overlay.class_sampling is not None
                else None
            )
            if sampling is None:
                return ModelCodeBudgetResult(
                    operation="sampling",
                    task_class=request.task_class,
                    call_budget_s=None,
                    source="overlay",
                    refusal=EnumCodeBudgetRefusal.UNNAMED_SAMPLING_CLASS,
                    completed_samples=0,
                    detail=f"No sampling named for task class: {request.task_class}",
                )
            return ModelCodeBudgetResult(
                operation="sampling",
                task_class=request.task_class,
                call_budget_s=None,
                source="overlay",
                refusal=None,
                completed_samples=0,
                detail="",
                sampling=sampling,
            )

        if request.operation == "output_budget":
            return self._output_budget(request)

        if request.operation == "input_ceiling":
            backend = request.backend or ""
            key = input_ceiling_overlay_key(backend, request.task_class)
            tokens = request.overlay.input_ceilings.get(backend, {}).get(
                request.task_class
            )
            if tokens is None:
                return ModelCodeBudgetResult(
                    operation="input_ceiling",
                    task_class=request.task_class,
                    call_budget_s=None,
                    source="overlay",
                    refusal=EnumCodeBudgetRefusal.UNNAMED_INPUT_CEILING,
                    completed_samples=0,
                    detail=f"No input ceiling named at overlay key {key}",
                    backend=backend,
                    overlay_key=key,
                )
            return ModelCodeBudgetResult(
                operation="input_ceiling",
                task_class=request.task_class,
                call_budget_s=None,
                source="overlay",
                refusal=None,
                completed_samples=0,
                detail="",
                backend=backend,
                input_ceiling_tokens=tokens,
                overlay_key=key,
            )

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

    def _output_budget(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        backend = request.backend or ""
        input_tokens = request.input_tokens or 0
        setting = (
            request.overlay.output_budgets.get(backend)
            if request.overlay.output_budgets is not None
            else None
        )
        if setting is None:
            return ModelCodeBudgetResult(
                operation="output_budget",
                task_class=request.task_class,
                call_budget_s=None,
                source="overlay",
                refusal=EnumCodeBudgetRefusal.UNNAMED_OUTPUT_BUDGET,
                completed_samples=0,
                detail=f"No output budget named for backend: {backend}",
                backend=backend,
            )
        room = setting.window - input_tokens - setting.margin
        if room < setting.floor:
            return ModelCodeBudgetResult(
                operation="output_budget",
                task_class=request.task_class,
                call_budget_s=None,
                source="overlay",
                refusal=EnumCodeBudgetRefusal.NO_OUTPUT_ROOM,
                completed_samples=0,
                detail=(
                    f"Room {room} is under the floor {setting.floor}: window "
                    f"{setting.window}, input {input_tokens}, margin {setting.margin}"
                ),
                backend=backend,
            )
        return ModelCodeBudgetResult(
            operation="output_budget",
            task_class=request.task_class,
            call_budget_s=None,
            source="overlay",
            refusal=None,
            completed_samples=0,
            detail="",
            backend=backend,
            output_budget=ModelOutputBudget(
                tokens=min(room, setting.ceiling),
                window=setting.window,
                input_tokens=input_tokens,
                margin=setting.margin,
                floor=setting.floor,
                ceiling=setting.ceiling,
            ),
        )
