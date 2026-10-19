# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why the Anthropic Messages API stopped generating (OMN-20910)."""

from __future__ import annotations

from enum import StrEnum, unique

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason


@unique
class EnumAnthropicStopReason(StrEnum):
    """The ``stop_reason`` of a native Messages API response.

    Typed so a parser never receives a bare string: ``MAX_TOKENS`` is the output
    budget cutting the reply off, which a review parser must read as a cut reply
    and never as a finished verdict. ``ABSENT`` (no field) and ``UNRECOGNISED``
    (a value this enum does not model) stay distinct, and neither one is
    evidence that the reply completed.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"
    PAUSE_TURN = "pause_turn"
    REFUSAL = "refusal"
    ABSENT = "absent"
    UNRECOGNISED = "unrecognised"

    @classmethod
    def parse(cls, raw: object) -> EnumAnthropicStopReason:
        """Classify one provider-reported stop reason."""
        if raw is None:
            return cls.ABSENT
        if not isinstance(raw, str):
            return cls.UNRECOGNISED
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return cls.UNRECOGNISED

    def to_finish_reason(self) -> EnumProviderFinishReason:
        """The OpenAI-compatible finish reason the same stop maps to."""
        return _FINISH_REASON[self]


_FINISH_REASON: dict[EnumAnthropicStopReason, EnumProviderFinishReason] = {
    EnumAnthropicStopReason.END_TURN: EnumProviderFinishReason.STOP,
    EnumAnthropicStopReason.STOP_SEQUENCE: EnumProviderFinishReason.STOP,
    EnumAnthropicStopReason.MAX_TOKENS: EnumProviderFinishReason.LENGTH,
    EnumAnthropicStopReason.TOOL_USE: EnumProviderFinishReason.TOOL_CALLS,
    EnumAnthropicStopReason.PAUSE_TURN: EnumProviderFinishReason.UNRECOGNISED,
    EnumAnthropicStopReason.REFUSAL: EnumProviderFinishReason.CONTENT_FILTER,
    EnumAnthropicStopReason.ABSENT: EnumProviderFinishReason.ABSENT,
    EnumAnthropicStopReason.UNRECOGNISED: EnumProviderFinishReason.UNRECOGNISED,
}

__all__ = ["EnumAnthropicStopReason"]
