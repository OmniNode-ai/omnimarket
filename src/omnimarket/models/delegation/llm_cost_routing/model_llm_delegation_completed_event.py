# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for a successfully completed LLM delegation call."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource


class ModelLlmDelegationCompletedEvent(BaseModel):
    """Event emitted when an LLM delegation call completes (success or escalated).

    Cost fields use Decimal for monetary precision. Raw prompt and response are
    not stored by default — only their SHA-256 hashes are recorded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    request_id: str
    task_type: str
    task_id: str | None

    # Model selection
    selected_model: str
    model_id: str
    model_tier: str
    provider: str
    endpoint_ref: str  # env var name, not raw URL

    # Token accounting
    tokens_in: int
    tokens_out: int
    latency_ms: int

    # Cost accounting (Decimal for monetary precision)
    actual_cost_usd: Decimal
    opus_equivalent_cost_usd: Decimal
    savings_usd: Decimal

    # Cost provenance
    usage_source: EnumUsageSource
    cost_basis: EnumCostBasis
    pricing_manifest_version: str
    pricing_manifest_hash: str

    # Content hashes (raw content NOT stored by default)
    output_hash: str  # SHA-256 of response content
    prompt_hash: str  # SHA-256 of prompt content

    # Routing provenance
    routing_policy_hash: str
    policy_hash: str  # alias for routing_policy_hash for convenience
    registry_hash: str  # hash of the model registry used for routing

    # Quality and escalation
    success: bool
    quality_score: float | None
    escalated_to: str | None
    escalation_reason: str | None
    redacted_summary: str | None  # short summary safe for logging

    created_at: datetime
