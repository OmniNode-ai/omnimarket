# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record every export reader produces, and the helpers they share."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from omnimarket.nodes.node_prompt_cost_estimate_compute.models import EnumTokenMethod

# A token count read from text length, not from the provider: about four
# characters per token for English prose and code. Used only where the export
# carries no counts, and every record it touches names its token method.
_CHARS_PER_TOKEN = 4


class ExportParseError(ValueError):
    """The upload is not the export shape it was declared as."""


@dataclass(frozen=True)
class ParsedPrompt:
    """One user prompt and the usage the export records for its answer."""

    conversation_id: str
    occurred_at: datetime | None
    prompt_text: str
    model: str
    tokens_in: int
    tokens_out: int
    token_method: EnumTokenMethod

    @property
    def tokens_from_text_length(self) -> int:
        """The part of this record's tokens that was read from text length."""
        method = self.token_method
        estimated_in = method in (
            EnumTokenMethod.TEXT_LENGTH_INPUT,
            EnumTokenMethod.TEXT_LENGTH_BOTH,
        )
        estimated_out = method in (
            EnumTokenMethod.TEXT_LENGTH_OUTPUT,
            EnumTokenMethod.TEXT_LENGTH_BOTH,
        )
        return (self.tokens_in if estimated_in else 0) + (
            self.tokens_out if estimated_out else 0
        )


def estimate_tokens(text: str) -> int:
    """Tokens for ``text`` by length; zero only for empty text."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN) if text else 0


def token_method(*, input_estimated: bool, output_estimated: bool) -> EnumTokenMethod:
    """The token method for a record whose input or output count was estimated."""
    if input_estimated and output_estimated:
        return EnumTokenMethod.TEXT_LENGTH_BOTH
    if input_estimated:
        return EnumTokenMethod.TEXT_LENGTH_INPUT
    if output_estimated:
        return EnumTokenMethod.TEXT_LENGTH_OUTPUT
    return EnumTokenMethod.REPORTED


def parse_iso(value: object) -> datetime | None:
    """An ISO-8601 timestamp as an aware UTC datetime, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
