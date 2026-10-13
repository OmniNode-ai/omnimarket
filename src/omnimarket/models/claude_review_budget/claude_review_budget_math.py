# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Token prices and the monthly cycle of the Claude review key budget (OMN-20967).

The input and output prices come from the omnibase_infra pricing manifest, the
one pricing authority. The manifest carries no cache prices, so a cache write
and a cache read are priced as multiples of the input price, the multiples
being contract config of the budget node. Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from decimal import Decimal

from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.claude_review_budget.model_claude_review_usage import (
    ModelClaudeReviewUsage,
)

_PER_1K_TOKENS = Decimal(1000)


class ModelClaudeReviewTokenPrices(BaseModel):
    """USD per token of one model, for each of the four usage counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(..., min_length=1)
    input_usd: Decimal = Field(..., ge=0)
    cache_write_usd: Decimal = Field(..., ge=0)
    cache_read_usd: Decimal = Field(..., ge=0)
    output_usd: Decimal = Field(..., ge=0)
    source: str = Field(..., description="Manifest source and effective date.")

    def cost_of(self, usage: ModelClaudeReviewUsage) -> Decimal:
        """What one call spent: each usage count times its price."""
        return (
            usage.input_tokens * self.input_usd
            + usage.cache_creation_input_tokens * self.cache_write_usd
            + usage.cache_read_input_tokens * self.cache_read_usd
            + usage.output_tokens * self.output_usd
        )

    def worst_case(self, *, prompt_tokens: int, max_tokens: int) -> Decimal:
        """The most a call can cost: the prompt uncached and the output at ``max_tokens``."""
        return prompt_tokens * self.input_usd + max_tokens * self.output_usd


def prices_from_manifest(
    table: ModelPricingTable,
    model_id: str,
    *,
    cache_write_input_multiplier: Decimal,
    cache_read_input_multiplier: Decimal,
) -> ModelClaudeReviewTokenPrices | None:
    """The model's per-token prices, or None when the manifest does not carry it."""
    entry = table.get_entry(model_id)
    if entry is None:
        return None
    input_usd = Decimal(str(entry.input_cost_per_1k)) / _PER_1K_TOKENS
    return ModelClaudeReviewTokenPrices(
        model_id=model_id,
        input_usd=input_usd,
        cache_write_usd=input_usd * cache_write_input_multiplier,
        cache_read_usd=input_usd * cache_read_input_multiplier,
        output_usd=Decimal(str(entry.output_cost_per_1k)) / _PER_1K_TOKENS,
        source=f"{entry.source}, effective {entry.effective_date}",
    )


def _add_months(anchor: datetime, months: int) -> datetime:
    """``anchor`` moved ``months`` calendar months on, the day clamped to the month's end."""
    index = anchor.month - 1 + months
    year, month = anchor.year + index // 12, index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def cycle_start(anchor: datetime, as_of: datetime, cycle_months: int) -> datetime:
    """The start of the cycle ``as_of`` falls in, cycles running every ``cycle_months`` from ``anchor``.

    Each start is computed from the anchor, never from the previous start, so
    an anchor on the 31st stays on the 31st wherever the month has one.
    """
    if as_of < anchor:
        raise ValueError(f"as_of {as_of.isoformat()} is before the cycle anchor")
    elapsed = (as_of.year - anchor.year) * 12 + as_of.month - anchor.month
    n = elapsed // cycle_months
    start = _add_months(anchor, n * cycle_months)
    while start > as_of:
        n -= 1
        start = _add_months(anchor, n * cycle_months)
    return start


__all__ = [
    "ModelClaudeReviewTokenPrices",
    "cycle_start",
    "prices_from_manifest",
]
