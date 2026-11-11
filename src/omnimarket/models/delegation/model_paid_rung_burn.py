# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The provider credit reading and the paid-rung burn reading (OMN-23756).

The operator asked how fast the hosted-model credit goes once the paid rungs
are on. The answer is read from two sources: the provider's own usage
accounting (its credits endpoint, total credits and total usage) and the
delegation call records (one per call, with the cost the provider reported).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ModelProviderCreditsReading(BaseModel):
    """The provider's own figures: credits bought and credits used, in USD."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_credits_usd: Decimal = Field(..., ge=0)
    total_usage_usd: Decimal = Field(..., ge=0)
    read_at: datetime

    @property
    def balance_usd(self) -> Decimal:
        """Credits left, floored at zero."""
        return max(self.total_credits_usd - self.total_usage_usd, Decimal(0))


class ModelPaidRungCallRecord(BaseModel):
    """One completed call on a paid rung, as the delegation event recorded it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    booked_cost_usd: Decimal = Field(..., ge=0)
    provider_reported_cost_usd: Decimal | None = Field(default=None, ge=0)
    completed_at: datetime


class ModelPaidRungModelBurn(BaseModel):
    """Calls, tokens and cost for one model since switch-on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    calls: int
    tokens_in: int
    tokens_out: int
    booked_cost_usd: Decimal
    provider_reported_cost_usd: Decimal | None


class ModelPaidRungBurnReading(BaseModel):
    """How fast the credit is going: one reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    enabled: bool
    enabled_at: datetime | None
    balance_usd: Decimal
    total_credits_usd: Decimal
    total_usage_usd: Decimal
    spend_cap_usd: Decimal
    spent_since_switch_on_usd: Decimal | None = Field(
        description="Provider usage now minus the baseline read at switch-on; None before switch-on."
    )
    usd_per_hour: Decimal | None
    hours_until_balance_gone: Decimal | None
    hours_until_cap_reached: Decimal | None
    per_model: tuple[ModelPaidRungModelBurn, ...]


__all__: list[str] = [
    "ModelPaidRungBurnReading",
    "ModelPaidRungCallRecord",
    "ModelPaidRungModelBurn",
    "ModelProviderCreditsReading",
]
