# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic contribution of one call to a UTC model/day aggregate."""

from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelUsageCallDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    usage_day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    model_id: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    occurred_at: AwareDatetime


__all__ = ["ModelUsageCallDelta"]
