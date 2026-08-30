# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The response shape a delegation request explicitly asked for (OMN-16932)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumRequestedResponseShape(StrEnum):
    """What the prompt itself declared about the shape of an adequate answer.

    The delegation quality gate scores a response against the task class's
    definition-of-done. That DoD describes the class (``research`` cites
    sources and reasons methodically), not the request. When a prompt
    explicitly constrains its own answer — "Reply with exactly the word:
    alive" — the class-level prose rubric is a category error: the model
    obeyed the instruction and the gate called the obedience ``WEAK_OUTPUT``,
    then climbed a paid ladder looking for a longer wrong answer.

    This enum is the typed, request-scoped expectation the routing reducer
    resolves from the prompt and the quality gate's DoD set is selected
    against. ``UNCONSTRAINED`` is the only value that leaves the class DoD
    untouched, so a prompt that declares nothing behaves exactly as before.
    """

    UNCONSTRAINED = "unconstrained"
    """The prompt declares no response shape; the task class DoD applies as-is."""

    SINGLE_WORD = "single_word"
    """The prompt asked for a one-word / single-token answer."""

    EXACT_LITERAL = "exact_literal"
    """The prompt named the exact text to reply with ("reply with exactly ...")."""


__all__: list[str] = ["EnumRequestedResponseShape"]
