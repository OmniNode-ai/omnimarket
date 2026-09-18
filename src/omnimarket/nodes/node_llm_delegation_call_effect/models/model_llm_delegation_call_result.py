# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the LLM delegation call effect node."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.inference.local_credential_refusal import (
    ModelLocalCredentialRefusal,
)


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

    # OMN-18278: why the provider stopped generating, read off
    # ``choices[0].finish_reason``. ``LENGTH`` means the output-token budget ran
    # out mid-generation, so the text on ``content`` is a fragment of a thought
    # rather than a finished answer — a fact no content heuristic can recover
    # once the response is separated from its envelope. The bus-less local
    # dispatch port threads this into the quality gate, which vetoes acceptance
    # on it.
    #
    # ``ABSENT`` is the honest value for a failure result (no provider choice
    # was ever parsed) and for a backend that omits the field. It is a record
    # that no signal accompanied the response — never a claim that the response
    # completed.
    finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.ABSENT

    # Quality gate result
    quality_score: float | None = None
    quality_gate_passed: bool = True

    # Populated on failure
    failure_class: EnumDelegationFailureClass | None = None
    error_message: str | None = None

    # OMN-18696: the typed credential refusal, populated ONLY when this failure
    # is one. It is not a second copy of the class -- ``failure_class`` above is
    # built from ``credential_refusal.failure_class`` when one is present, so
    # the two cannot disagree. Callers that only read ``failure_class`` keep
    # working; the escalation ladder and the CLI read this for the reference
    # name, the remediation and the non-retryable verdict, none of which a
    # failure class alone can carry. ``None`` on every other failure and on
    # every success -- a refusal is a fact about a credential, never a default.
    credential_refusal: ModelLocalCredentialRefusal | None = None

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
