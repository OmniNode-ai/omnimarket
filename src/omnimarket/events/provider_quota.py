# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider quota observation event (OMN-20154).

One event per metered provider call: the call happened, and what the provider
said about capacity. The durable quota state is the projection of these events
(``node_projection_provider_quota``), keyed by
``(tenant_id, credential_ref, provider_id, model_scope)``. Routing and the
judge read that projection; nothing keeps quota state in process memory.

The lab is one tenant among many here, never a separate path: a lab lane's
delegation and a customer's delegation emit the same event and land in the
same table under their own tenant.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

#: ``model_scope`` of a row that bars every model behind the credential.
PROVIDER_WIDE_MODEL_SCOPE = "*"

#: ``credential_ref`` of a call made with no credential reference at all.
UNAUTHENTICATED_CREDENTIAL_REF = "unauthenticated"


class EnumProviderQuotaOutcome(StrEnum):
    """What one provider call returned, as far as capacity is concerned."""

    CALL_OK = "call_ok"
    """The provider answered. Counts as a call; clears an older block on the key."""

    CALL_FAILED = "call_failed"
    """The call failed for a reason that is not a quota refusal. Counts as a call."""

    LIMIT_HIT = "limit_hit"
    """The provider refused for capacity. Counts as a call and records a block."""


class EnumProviderQuotaSource(StrEnum):
    """Which call path observed the call. Provenance only; the row is the same."""

    RUNTIME_ORCHESTRATOR = "runtime_orchestrator"
    INPROCESS_EFFECT = "inprocess_effect"
    JUDGE = "judge"


def credential_ref_for(api_key_ref: str | None) -> str:
    """The credential key component for a call's credential reference.

    The reference NAME, never a value. A call made with no reference is keyed
    as ``unauthenticated`` so it can never share a row with a keyed call.
    """
    ref = (api_key_ref or "").strip()
    return ref or UNAUTHENTICATED_CREDENTIAL_REF


class ModelProviderQuotaObserved(BaseModel):
    """One metered provider call and its capacity outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(..., description="Producer-assigned; stable across retries.")
    tenant_id: UUID = Field(..., description="The tenant whose call this was.")
    credential_ref: str = Field(
        ...,
        min_length=1,
        description="Credential reference name (never a value); see credential_ref_for.",
    )
    provider_id: str = Field(
        ...,
        min_length=1,
        description="Quota domain: the policy's provider_id, or host:<host> when undeclared.",
    )
    model_name: str = Field(..., min_length=1, description="The model that was called.")
    outcome: EnumProviderQuotaOutcome
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = Field(
        default=None,
        description="Provider-native code (z.ai 1302, RESOURCE_EXHAUSTED).",
    )
    disposition: str | None = Field(
        default=None,
        description="EnumQuotaDisposition value of a LIMIT_HIT; None otherwise.",
    )
    block_scope: str | None = Field(
        default=None,
        description="EnumQuotaScope value of a LIMIT_HIT (provider or model).",
    )
    blocked_until: AwareDatetime | None = Field(
        default=None,
        description="Instant the key returns to routing; None with blocked_indefinitely.",
    )
    blocked_indefinitely: bool = Field(
        default=False,
        description="A billing/entitlement refusal: no reset is coming (alert).",
    )
    reason: str = Field(default="", max_length=2000)
    call_started_at: AwareDatetime = Field(
        ...,
        description=(
            "When the call was sent. A CALL_OK clears only a block recorded "
            "BEFORE this instant, so a call already in flight when the "
            "provider refused cannot lift the refusal."
        ),
    )
    observed_at: AwareDatetime = Field(
        ..., description="When the outcome was observed (producer clock)."
    )
    window_seconds: int = Field(
        default=60,
        ge=1,
        description="Width of the rolling call-count window the row keeps.",
    )
    correlation_id: UUID | None = Field(default=None)
    source: EnumProviderQuotaSource

    @model_validator(mode="after")
    def _a_limit_hit_names_its_block(self) -> ModelProviderQuotaObserved:
        if self.model_name == PROVIDER_WIDE_MODEL_SCOPE:
            raise ValueError(
                f"model_name {PROVIDER_WIDE_MODEL_SCOPE!r} is the provider-wide "
                "row's scope, never a model"
            )
        if self.outcome is EnumProviderQuotaOutcome.LIMIT_HIT:
            if self.block_scope not in ("provider", "model"):
                raise ValueError("a LIMIT_HIT names block_scope provider or model")
            if self.blocked_until is None and not self.blocked_indefinitely:
                raise ValueError(
                    "a LIMIT_HIT carries blocked_until or blocked_indefinitely"
                )
        elif (
            self.blocked_until is not None
            or self.blocked_indefinitely
            or self.block_scope is not None
        ):
            raise ValueError("only a LIMIT_HIT carries a block")
        return self


__all__ = [
    "PROVIDER_WIDE_MODEL_SCOPE",
    "UNAUTHENTICATED_CREDENTIAL_REF",
    "EnumProviderQuotaOutcome",
    "EnumProviderQuotaSource",
    "ModelProviderQuotaObserved",
    "credential_ref_for",
]
