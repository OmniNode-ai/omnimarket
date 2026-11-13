# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The call budget of a task class, read from the code budget overlay (OMN-24023).

The lane harness asks this reader for the call budget of a task class before it
delegates a draft. The budget is a value of ``node_delegation_code_budget_
compute``'s overlay (``class_budgets_s``), read through that node's ``resolve``
operation; nothing here has a default for a class. A class the overlay does not
name, and an install no overlay root names the code budget overlay on (a
customer's clean install), have no budget, so the reader returns ``None``; an
overlay that is present but unreadable or invalid raises
:class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a
default. The overlay is read on every call, so a changed budget is in force on
the next read.

The strict reader is for a caller that must not run without a declared budget
(the inference effect, OMN-24039).
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)


def read_call_budget_s(
    task_class: str, environ: Mapping[str, str] | None = None
) -> float | None:
    """The overlay's call budget for ``task_class``, or ``None`` when it names none.

    Read through ``node_delegation_code_budget_compute``'s ``resolve`` operation.
    A class the overlay does not name has no budget, and an install no overlay
    root names the code budget overlay on declares no budgets at all. An overlay
    that is present but unreadable or invalid raises
    :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a
    default.
    """
    try:
        overlay = load_structured_overlay(
            CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None
    result = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="resolve",
            task_class=task_class,
            overlay=overlay,
        )
    )
    if result.refusal is EnumCodeBudgetRefusal.UNKNOWN_TASK_CLASS:
        return None
    return result.call_budget_s


class CallBudgetNotDeclaredError(ValueError):
    """The code budget overlay names no call budget for a task class."""


def require_call_budget_s(
    task_class: str, environ: Mapping[str, str] | None = None
) -> float:
    """The overlay's call budget for ``task_class``; refuses when it names none.

    Refuses with :class:`CallBudgetNotDeclaredError` when the overlay names no
    budget for ``task_class``, when no overlay root names the code budget
    overlay, and when ``task_class`` is blank. Same reading as
    :func:`read_call_budget_s` (it calls that reader, it does not reimplement
    the node read); an invalid overlay still raises
    :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`.
    """
    value = read_call_budget_s(task_class, environ) if task_class.strip() else None
    if value is None:
        raise CallBudgetNotDeclaredError(
            f"no call budget declared for task class {task_class!r} in the "
            f"code budget overlay {CODE_BUDGET_OVERLAY_ID!r}; the budget is an "
            f"overlay value with no code default"
        )
    return value


def require_call_ceiling_s(environ: Mapping[str, str] | None = None) -> float:
    """The code budget overlay's ceiling; refuses when no overlay root names it.

    Refuses with :class:`CallBudgetNotDeclaredError` when no overlay root names
    the code budget overlay; the ceiling is an overlay value with no code
    default. An invalid overlay still raises
    :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`.
    """
    try:
        overlay = load_structured_overlay(
            CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, environ
        )
    except NodeOverlayNotFoundError as exc:
        raise CallBudgetNotDeclaredError(
            f"no code budget overlay is declared in {CODE_BUDGET_OVERLAY_ID!r}; "
            f"the ceiling is an overlay value with no code default"
        ) from exc
    return overlay.ceiling_s


__all__ = [
    "CallBudgetNotDeclaredError",
    "read_call_budget_s",
    "require_call_budget_s",
    "require_call_ceiling_s",
]
