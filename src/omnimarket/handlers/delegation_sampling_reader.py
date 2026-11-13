# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sampling settings of a task class, read from the code budget overlay.

The lane harness asks this reader for the sampling settings of a task class before it
delegates a draft. The settings are a value of ``node_delegation_code_budget_compute``'
overlay (``class_sampling``), read through that node's ``sampling`` operation; nothing
here has a default for a class. A class the overlay does not name, and an install no
overlay root names the code budget overlay on (a customer's clean install), have no
sampling settings, so the reader returns ``None``; an overlay that is present but
unreadable or invalid raises
:class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a default. The
overlay is read on every call, so changed settings are in force on the next read.
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
    ModelClassSampling,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)


def read_sampling_settings(
    task_class: str, environ: Mapping[str, str] | None = None
) -> ModelClassSampling | None:
    """The overlay's sampling settings for ``task_class``, or ``None`` when it names none.

    Read through ``node_delegation_code_budget_compute``'s ``sampling`` operation. A
    class the overlay does not name has no settings, and an install no overlay root
    names the code budget overlay on declares no settings at all. An overlay that is
    present but unreadable or invalid raises
    :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a default.
    """
    try:
        overlay = load_structured_overlay(
            CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None
    result = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="sampling",
            task_class=task_class,
            overlay=overlay,
        )
    )
    if result.refusal is EnumCodeBudgetRefusal.UNNAMED_SAMPLING_CLASS:
        return None
    return result.sampling


__all__ = ["read_sampling_settings"]
