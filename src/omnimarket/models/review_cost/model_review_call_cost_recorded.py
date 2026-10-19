# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cost of one provider attempt of a Hostile Review call (OMN-20910).

One record per provider attempt, made where the delegation call effect already
reports each attempt (``started`` and ``finished``), so an invocation that is
cancelled after some attempts still leaves one record for each attempt it
made. The record is idempotent on (call id, attempt): its event id is derived
from that pair, so a redelivery names the same event.

A key is named by its reference, never its value.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_review_call_outcome import EnumReviewCallOutcome
from omnimarket.models.review_cost.model_review_call_provenance import (
    ModelReviewCallProvenance,
)

_FINISH_REASON_LENGTH = "length"


def review_call_cost_event_id(call_id: str, attempt: int) -> UUID:
    """The event id of attempt ``attempt`` of call ``call_id``; stable across replays."""
    return uuid5(NAMESPACE_URL, f"review-call-cost:{call_id}:{attempt}")


def review_call_outcome(
    *,
    success: bool,
    failure_class: EnumDelegationFailureClass | None,
    finish_reason: str | None,
) -> EnumReviewCallOutcome:
    """Classify a finished attempt from what the provider and the effect reported."""
    if success:
        if finish_reason == _FINISH_REASON_LENGTH:
            return EnumReviewCallOutcome.TRUNCATED
        return EnumReviewCallOutcome.OK
    if failure_class is EnumDelegationFailureClass.CONTEXT_TOO_LARGE:
        return EnumReviewCallOutcome.OVERFLOW
    if failure_class is EnumDelegationFailureClass.INVALID_JSON:
        return EnumReviewCallOutcome.SCHEMA_INVALID
    return EnumReviewCallOutcome.UPSTREAM_ERROR


def _host_of(endpoint_url: str | None) -> str | None:
    if not endpoint_url:
        return None
    return urlparse(endpoint_url).hostname or None


class ModelReviewCallCostRecorded(BaseModel):
    """Usage, outcome and priced cost of one provider attempt of a review call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    call_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    recorded_at: AwareDatetime
    provenance: ModelReviewCallProvenance

    requested_model: str = Field(min_length=1)
    served_model: str | None = None
    key_ref: str | None = None
    host: str | None = None

    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    # None means the provider reported no figure; it is never read as zero, so a
    # provider that has no cache is not counted as a cache miss.
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_5m_tokens: int | None = Field(default=None, ge=0)
    cache_write_1h_tokens: int | None = Field(default=None, ge=0)

    stop_reason: str | None = None
    outcome: EnumReviewCallOutcome
    latency_ms: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    pricing_manifest_version: str = ""

    @classmethod
    def for_finished_attempt(
        cls,
        *,
        call_id: str,
        attempt: int,
        recorded_at: AwareDatetime,
        provenance: ModelReviewCallProvenance,
        requested_model: str,
        served_model: str | None,
        key_ref: str | None,
        endpoint_url: str | None,
        success: bool,
        failure_class: EnumDelegationFailureClass | None,
        finish_reason: str | None,
        tokens_in: int,
        tokens_out: int,
        cache_read_tokens: int | None,
        cache_write_5m_tokens: int | None,
        cache_write_1h_tokens: int | None,
        latency_ms: int,
        cost_usd: Decimal,
        pricing_manifest_version: str,
    ) -> ModelReviewCallCostRecorded:
        """The record of an attempt the provider answered, or failed, in time."""
        return cls(
            event_id=review_call_cost_event_id(call_id, attempt),
            call_id=call_id,
            attempt=attempt,
            recorded_at=recorded_at,
            provenance=provenance,
            requested_model=requested_model,
            served_model=served_model,
            key_ref=key_ref,
            host=_host_of(endpoint_url),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
            stop_reason=finish_reason,
            outcome=review_call_outcome(
                success=success,
                failure_class=failure_class,
                finish_reason=finish_reason,
            ),
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            pricing_manifest_version=pricing_manifest_version,
        )

    @classmethod
    def for_cancelled_attempt(
        cls,
        *,
        call_id: str,
        attempt: int,
        recorded_at: AwareDatetime,
        provenance: ModelReviewCallProvenance,
        requested_model: str,
        key_ref: str | None,
        endpoint_url: str | None,
        elapsed_ms: int,
        pricing_manifest_version: str,
    ) -> ModelReviewCallCostRecorded:
        """The record of an attempt that had left but never answered when the call ended.

        Its usage is unknown, so it carries zero tokens and zero cost and the
        outcome says why; a projection counts it as an attempt, not as spend.
        """
        return cls(
            event_id=review_call_cost_event_id(call_id, attempt),
            call_id=call_id,
            attempt=attempt,
            recorded_at=recorded_at,
            provenance=provenance,
            requested_model=requested_model,
            key_ref=key_ref,
            host=_host_of(endpoint_url),
            outcome=EnumReviewCallOutcome.CANCELLED,
            latency_ms=elapsed_ms,
            pricing_manifest_version=pricing_manifest_version,
        )


__all__ = [
    "ModelReviewCallCostRecorded",
    "review_call_cost_event_id",
    "review_call_outcome",
]
