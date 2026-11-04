# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure unanswered-message handlers, loaded when their contract route is used."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .handler_answer_words import HandlerAnswerWords
    from .handler_operator_unanswered import HandlerOperatorUnanswered
    from .handler_structural_miss_rate import HandlerStructuralMissRate

__all__ = [
    "HandlerAnswerWords",
    "HandlerOperatorUnanswered",
    "HandlerStructuralMissRate",
]

_HANDLERS = {
    "HandlerAnswerWords": ".handler_answer_words",
    "HandlerOperatorUnanswered": ".handler_operator_unanswered",
    "HandlerStructuralMissRate": ".handler_structural_miss_rate",
}


def __getattr__(name: str) -> object:
    module = _HANDLERS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
