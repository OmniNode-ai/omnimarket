# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thinking-budget bound for the local delegation dispatch (OMN-26588)."""

from __future__ import annotations

from typing import Any

_THINKING_BUDGET_OPTION_KEYS: tuple[str, ...] = (
    "thinking_token_budget",
    "thinking_budget_tokens",
)


def bound_thinking_budget_to_answer_room(
    request_options: dict[str, Any], *, max_tokens: int
) -> dict[str, Any]:
    """The request options with each thinking budget cut to half the output cap (OMN-26588).

    Thinking tokens come out of the same completion budget as the answer, so a
    thinking budget at or above the output cap leaves the answer nothing: the model
    reasons until ``length`` and returns empty content. Half the cap keeps at least
    as many tokens for the answer as for the reasoning, whoever set the budget (a
    profile or the code-budget overlay). A budget already within half the cap, and
    options with no thinking budget, come back equal; the options passed in are
    never changed.
    """
    ceiling = max(max_tokens // 2, 1)
    bounded = dict(request_options)
    for key in _THINKING_BUDGET_OPTION_KEYS:
        value = bounded.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > ceiling:
            bounded[key] = ceiling
    return bounded


__all__ = ["bound_thinking_budget_to_answer_room"]
