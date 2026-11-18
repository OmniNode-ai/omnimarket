# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine order of a delegation step, from the routing order reader (OMN-24044).

A step asks for its first engine: ``text`` drafts on ``lab`` or ``glm`` first, and ``code`` runs GLM ``before-lab`` or
``last``. An explicit flag wins. Without one the order is the routing overlay's chain for the task class, read through
:func:`read_engine_order` (the reader names the lab tier ``ladder`` and a harness rung by its harness name): the first lab or
GLM rung for text, and GLM ahead of the lab tier or not for code. A class the overlay names no chain for, an overlay no root
binds, a reader that raises, or a chain that does not place the engines keeps the step's old default and says why, so a bad
overlay never stops a step.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
    read_engine_order,
)
from omnimarket.models.delegation.model_delegation_step_order import (
    ModelDelegationStepOrder,
)

STEP_KINDS = ("text", "code")
OLD_DEFAULTS = {"text": "glm", "code": "before-lab"}
LAB_RUNG = "ladder"
GLM_RUNG = "claude-glm"


def _text_first(order: tuple[str, ...]) -> str | None:
    """The first lab or GLM rung of the order; a rung of another harness is passed over."""
    return next(
        (
            "lab" if rung == LAB_RUNG else "glm"
            for rung in order
            if rung in (LAB_RUNG, GLM_RUNG)
        ),
        None,
    )


def _code_glm(order: tuple[str, ...]) -> str | None:
    """``before-lab`` when GLM stands ahead of the lab tier, ``last`` when the lab tier comes first, None with no lab tier."""
    if LAB_RUNG not in order:
        return None
    return "before-lab" if GLM_RUNG in order[: order.index(LAB_RUNG)] else "last"


def step_order(
    kind: str,
    task_class: str,
    flag: str | None = None,
    *,
    read: Callable[[str], tuple[str, ...] | None] | None = None,
) -> ModelDelegationStepOrder:
    """The order a ``kind`` step runs for ``task_class``: the flag, else the overlay's order, else the old default."""
    if kind not in STEP_KINDS:
        raise ValueError(f"kind must be one of {STEP_KINDS}, got {kind!r}")
    if flag is not None:
        return ModelDelegationStepOrder(choice=flag, order_source="flag")
    why = ""
    order: tuple[str, ...] | None = None
    try:
        order = (read or read_engine_order)(task_class)
    except DelegationRoutingOrderError as exc:
        why = f"routing order reader refused: {exc}"
    if order is None:
        why = why or f"the overlay names no engine order for {task_class}"
    else:
        choice = (_text_first if kind == "text" else _code_glm)(order)
        if choice is not None:
            return ModelDelegationStepOrder(
                choice=choice, order_source="routing_order_node", routing_order=order
            )
        why = f"the overlay order {list(order)} does not place the engines"
    return ModelDelegationStepOrder(
        choice=OLD_DEFAULTS[kind], order_source="default", order_fallback=why
    )
