# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One native Anthropic Messages reply, read (OMN-20910)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_anthropic_stop_reason import EnumAnthropicStopReason

#: Input-token price multipliers of the provider's published cache pricing: a
#: 5-minute write costs 1.25x, a 1-hour write 2x and a read 0.1x the base input
#: price. Mirrors node_claude_review_budget_compute's contract values.
CACHE_WRITE_5M_MULTIPLIER = Decimal("1.25")
CACHE_WRITE_1H_MULTIPLIER = Decimal("2.0")
CACHE_READ_MULTIPLIER = Decimal("0.1")


class ModelAnthropicMessagesReading(BaseModel):
    """What one native reply said: text, stop reason and measured usage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    #: Where ``content`` came from, for the raw-response record.
    source_field: str
    stop_reason: EnumAnthropicStopReason
    served_model: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_5m_input_tokens: int = Field(ge=0)
    cache_creation_1h_input_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)

    @property
    def prompt_tokens(self) -> int:
        """The whole prompt: uncached, written to the cache and read from it."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def input_price_equivalent_tokens(self) -> int:
        """Input tokens at the base price that bill the same as this prompt."""
        weighted = (
            Decimal(self.input_tokens)
            + Decimal(self.cache_creation_5m_input_tokens) * CACHE_WRITE_5M_MULTIPLIER
            + Decimal(self.cache_creation_1h_input_tokens) * CACHE_WRITE_1H_MULTIPLIER
            + Decimal(self.cache_read_input_tokens) * CACHE_READ_MULTIPLIER
        )
        return int(weighted.to_integral_value(rounding=ROUND_HALF_UP))


__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_WRITE_5M_MULTIPLIER",
    "ModelAnthropicMessagesReading",
]
