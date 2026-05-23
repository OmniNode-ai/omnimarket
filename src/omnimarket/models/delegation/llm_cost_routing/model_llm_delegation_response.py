# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for the LLM cost-routing delegation framework."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource


class ModelLlmDelegationResponse(BaseModel):
    """Output from the LLM delegation routing pipeline.

    Carries full cost provenance fields so every response can be attributed
    to the correct pricing manifest version and cost basis.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    content: str
    """Generated content from the delegated model."""

    model_id: str
    model_tier: str
    tokens_in: int
    tokens_out: int
    latency_ms: int

    # Cost fields use Decimal for monetary precision
    actual_cost_usd: Decimal
    opus_equivalent_cost_usd: Decimal

    usage_source: EnumUsageSource
    cost_basis: EnumCostBasis
    pricing_manifest_version: str

    escalated: bool
    quality_score: float | None = None

    output_hash: str
    """SHA-256 of content. Used for idempotency and audit."""

    redacted_summary: str | None = None
    """Short summary safe for logging. Raw content NOT stored by default."""
