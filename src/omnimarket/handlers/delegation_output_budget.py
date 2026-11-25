# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output budget of one delegated request, read from the code budget overlay (OMN-26640).

A backend the overlay names in ``output_budgets`` is sent each request with the window less the request's
measured input tokens less the declared margin, held to the declared ceiling (``node_delegation_code_budget_compute``'
``output_budget`` operation). The window, margin, floor and ceiling are the overlay's; the input count is the
served model's own, from the injected meter. When the meter cannot answer the count falls back to the router's
tokenizer-free upper bound, which never undercounts, so the budget errs small and the request still fits; the
resolution says which source it used. A backend the overlay does not name, and an install no overlay root names the
code budget overlay on, have no budget here and keep the routing contract's. An overlay that is present but
unreadable raises :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a default. The overlay is
read on every call, so changed settings are in force on the next request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.routing.input_token_bound import input_token_upper_bound
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.handlers.routing.vllm_input_token_meter import InputTokenMeterError
from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
    ModelOutputBudgetResolution,
    ModelOutputBudgetSetting,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)
from omnimarket.protocols.protocol_input_token_meter import ProtocolInputTokenMeter

#: The task class the node's request needs; the operation is per backend and ignores it.
_OUTPUT_BUDGET_TASK_CLASS = "output_budget"


class OutputBudgetRefusedError(ValueError):
    """The request leaves no room for the declared floor of output."""


def _load_overlay(environ: Mapping[str, str] | None) -> ModelCodeBudgetOverlay | None:
    try:
        return load_structured_overlay(
            CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None


def read_output_budget_setting(
    backend_id: str, environ: Mapping[str, str] | None = None
) -> ModelOutputBudgetSetting | None:
    """The overlay's output budget setting for ``backend_id``, or ``None`` when it names none."""
    overlay = _load_overlay(environ)
    if overlay is None or overlay.output_budgets is None:
        return None
    return overlay.output_budgets.get(backend_id)


def _messages_upper_bound(messages: Sequence[Mapping[str, str]]) -> int:
    return sum(input_token_upper_bound(m.get("content", "")) for m in messages)


def resolve_output_budget(
    *,
    backend_id: str,
    endpoint_ref: str,
    model_id: str,
    messages: Sequence[Mapping[str, str]],
    requested: int | None,
    meter: ProtocolInputTokenMeter,
    environ: Mapping[str, str] | None = None,
) -> ModelOutputBudgetResolution | None:
    """The budget ``messages`` are sent with on ``backend_id``, or ``None`` when the overlay names none.

    ``requested`` is the caller's own cap: a caller may ask for fewer tokens than the budget, never more.
    Raises :class:`OutputBudgetRefusedError` when the room is under the overlay's floor.
    """
    overlay = _load_overlay(environ)
    if overlay is None or overlay.output_budgets is None:
        return None
    setting = overlay.output_budgets.get(backend_id)
    if setting is None:
        return None
    try:
        input_tokens = meter.count(
            endpoint_ref=endpoint_ref,
            model_id=model_id,
            messages=messages,
            timeout_s=setting.measure_timeout_s,
        )
        source = "measured"
    except InputTokenMeterError:
        input_tokens = _messages_upper_bound(messages)
        source = "upper_bound"
    result = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="output_budget",
            task_class=_OUTPUT_BUDGET_TASK_CLASS,
            backend=backend_id,
            input_tokens=input_tokens,
            overlay=overlay,
        )
    )
    if result.refusal is EnumCodeBudgetRefusal.NO_OUTPUT_ROOM:
        raise OutputBudgetRefusedError(
            f"prompt leaves no output room on {backend_id}: {result.detail}"
        )
    budget = result.output_budget
    if budget is None:
        raise OutputBudgetRefusedError(
            f"no output budget for {backend_id}: {result.detail}"
        )
    return ModelOutputBudgetResolution(
        tokens=budget.tokens if requested is None else min(requested, budget.tokens),
        window=budget.window,
        input_tokens=input_tokens,
        input_source="measured" if source == "measured" else "upper_bound",
        margin=budget.margin,
        floor=budget.floor,
        ceiling=budget.ceiling,
        requested=requested,
    )


__all__ = [
    "OutputBudgetRefusedError",
    "read_output_budget_setting",
    "resolve_output_budget",
]
