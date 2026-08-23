# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the LLM delegation call effect node."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource


class ModelLlmDelegationCallResult(BaseModel):
    """Result from HandlerLlmDelegationCall after one LLM API call attempt.

    On success, content and cost fields are populated. On failure, failure_class
    and error_message are populated and content is None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    request_id: str
    success: bool

    # Populated on success
    content: str | None = None
    output_hash: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0

    # Cost telemetry (populated on success from MEASURED API response)
    actual_cost_usd: Decimal = Decimal("0")
    opus_equivalent_cost_usd: Decimal = Decimal("0")
    savings_usd: Decimal = Decimal("0")
    usage_source: EnumUsageSource = EnumUsageSource.UNKNOWN
    cost_basis: EnumCostBasis = EnumCostBasis.UNKNOWN

    # Quality gate result
    quality_score: float | None = None
    quality_gate_passed: bool = True

    # Populated on failure
    failure_class: EnumDelegationFailureClass | None = None
    error_message: str | None = None

    # Health probe outcome (informational)
    endpoint_healthy: bool = True

    # OMN-16419: the model id CONFIRMED served by the endpoint's own
    # GET /v1/models at call time, populated only when that probe succeeded and
    # matched ``request.model_id`` (the fail-closed guard in
    # HandlerLlmDelegationCall rejects the call before reaching here on a
    # mismatch). ``None`` means the probe found no evidence either way (e.g. a
    # cloud backend that doesn't expose this path) — callers fall back to the
    # configured name in that case, preserving OMN-8022's behavior. When set,
    # this is MORE TRUSTWORTHY attribution than the configured name alone and
    # should be preferred for event/receipt attribution.
    served_model_id: str | None = None
