# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why an OpenAI-compatible provider stopped generating (OMN-18278)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumProviderFinishReason(StrEnum):
    """The ``choices[].finish_reason`` an OpenAI-compatible provider reported.

    ``ABSENT`` and ``UNRECOGNISED`` are deliberately two members rather than
    one. "The provider reported nothing" and "the provider reported something
    this contract does not model" are different facts about the wire, and
    collapsing them would make a newly-introduced vendor value indistinguishable
    from a field that was never sent. Neither one is evidence that the response
    completed: only :attr:`STOP` says that, and only :attr:`LENGTH` says the
    output budget cut the model off mid-generation.
    """

    STOP = "stop"
    """The model emitted its own stop condition: the answer is finished."""

    LENGTH = "length"
    """The output-token budget ran out before the model finished."""

    CONTENT_FILTER = "content_filter"
    """The provider's own safety filter halted generation."""

    TOOL_CALLS = "tool_calls"
    """The model stopped to call a tool."""

    FUNCTION_CALL = "function_call"
    """The legacy single-function form of :attr:`TOOL_CALLS`."""

    ABSENT = "absent"
    """The provider's choice carried no ``finish_reason`` field at all."""

    UNRECOGNISED = "unrecognised"
    """The provider reported a reason this contract does not model."""


__all__ = ["EnumProviderFinishReason"]
