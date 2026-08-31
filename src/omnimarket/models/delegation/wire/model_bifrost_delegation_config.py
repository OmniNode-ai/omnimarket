# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Bifrost delegation config wire DTOs."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDelegationShadowConfig(BaseModel):
    """Shadow routing comparison settings."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    enabled: bool = Field(default=False, description="Whether shadow mode is active.")
    policy_version: str = Field(
        default="unknown",
        max_length=128,
        description="Human-readable version of the loaded shadow policy checkpoint.",
    )
    log_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of requests to log shadow decisions for.",
    )
    comparison_logging_enabled: bool = Field(
        default=True,
        description="Whether to emit comparison events for shadow decisions.",
    )
    max_shadow_latency_ms: float = Field(
        default=5.0,
        ge=0.1,
        le=100.0,
        description="Maximum allowed latency for shadow policy evaluation (ms).",
    )
    shadow_label: Literal["SHADOW"] = Field(
        default="SHADOW",
        description="Label applied to all shadow comparison events.",
    )


class ModelDelegationFallbackPolicy(BaseModel):
    """Per-rule fallback behavior when backend attempts fail."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    action: str = Field(
        ...,
        description="Action on backend failure: 'escalate_to_next_tier' or 'return_error'.",
    )
    max_retries: int = Field(
        default=1,
        ge=0,
        le=10,
        description="Maximum retry attempts across backends in this rule.",
    )
    on_exhaust: str = Field(
        default="return_error",
        description="Behavior when all retries are exhausted.",
    )


class ModelDelegationRoutingRule(BaseModel):
    """Routing rule from task class constraints to ordered backend IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    rule_id: UUID = Field(
        ..., description="Stable UUID for audit logging and provenance."
    )
    priority: int = Field(
        default=100,
        ge=0,
        description="Evaluation order - lower values evaluated first.",
    )
    task_class: str = Field(..., description="Task class this rule targets.")
    task_class_contract_version: str = Field(
        ...,
        description="Version of the task class contract this rule was authored against.",
    )
    backend_policy_version: str = Field(
        ...,
        description="Version of the backend policy applied by this rule.",
    )
    match_operation_types: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Operation types this rule matches (empty = any).",
    )
    match_capabilities: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Capabilities the request must declare (empty = any).",
    )
    latency_sla_ms: int | None = Field(
        default=None,
        ge=1,
        description="SLA latency constraint.",
    )
    cost_ceiling_usd_per_1k_tokens: float | None = Field(
        default=None,
        ge=0.0,
        description="Maximum allowed cost per 1K tokens for this rule's backends.",
    )
    backend_ids: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Ordered backend IDs to try when this rule matches.",
    )
    fallback_policy: ModelDelegationFallbackPolicy = Field(
        ...,
        description="Failover behavior when backends are exhausted.",
    )
    shadow_policy_id: UUID = Field(
        ..., description="Shadow policy UUID for A/B evaluation."
    )


class ModelDelegationBackendConfig(BaseModel):
    """Backend definition for the Bifrost delegation gateway."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    backend_id: str = Field(
        ..., min_length=1, description="Stable human-readable slug."
    )
    endpoint_url_env: str | None = Field(
        default=None,
        description=(
            "Env var name holding the COMPLETE endpoint URL for local backends "
            "(incl. /v1/chat/completions). Resolved verbatim — no construction "
            "(OMN-13159 / OMN-12815)."
        ),
    )
    endpoint_url: str | None = Field(
        default=None,
        description="COMPLETE endpoint URL (incl. the full chat/completions path) populated by the deploy-time overlay, posted verbatim. Null for local backends until the overlay is applied.",
    )
    model_name: str | None = Field(
        default=None,
        description="Model identifier sent in outbound requests. Null for local backends resolved at deploy time.",
    )
    api_key_env: str | None = Field(
        default=None,
        description=(
            "Legacy environment variable name holding the backend API key. "
            "Use secret_ref for new backends."
        ),
    )
    api_key_ref: str | None = Field(
        default=None,
        description=(
            "Legacy non-secret API-key reference. Use secret_ref for new backends."
        ),
    )
    secret_ref: str | None = Field(
        default=None,
        description=(
            "Logical secret reference resolved through the lane secret mapping "
            "and ProtocolSecretStore at the effect boundary."
        ),
    )
    extra_headers: dict[str, str] | None = Field(
        default=None,
        description="Optional static HTTP headers required by the backend provider.",
    )
    tier: str = Field(..., description="Routing tier: 'local' or 'frontier_api'.")
    timeout_ms: int = Field(
        default=30000,
        ge=100,
        le=600000,
        description="Per-backend HTTP timeout in milliseconds.",
    )
    max_tokens: int = Field(
        default=65536,
        ge=1,
        le=200000,
        description=(
            "Per-backend output-token budget/ceiling, resolved from the routing "
            "contract (overlay-overridable). Bounded by the backend model's "
            "context window. Local Qwen 128k backends carry 65536; cloud backends "
            "carry their real provider output ceiling (OMN-13161)."
        ),
    )
    capabilities: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Capabilities this backend supports.",
    )

    @model_validator(mode="after")
    def _validate_secret_ref_fields(self) -> ModelDelegationBackendConfig:
        """Reject ambiguous canonical secret refs while allowing migration aliases."""
        canonical_refs = {
            value
            for value in (self.secret_ref, self.api_key_ref)
            if value is not None and value.strip()
        }
        if len(canonical_refs) > 1:
            msg = "secret_ref and api_key_ref must match when both are declared"
            raise ValueError(msg)
        return self

    @property
    def resolved_secret_ref(self) -> str | None:
        """Return the canonical non-secret reference for this backend."""
        return self.secret_ref or self.api_key_ref or self.api_key_env


class ModelDelegationCircuitBreakerConfig(BaseModel):
    """Circuit breaker settings applying to all Bifrost backends."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    failure_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Consecutive failures that open the circuit.",
    )
    window_seconds: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Cooldown duration after circuit opens, in seconds.",
    )


class ModelDelegationFailoverConfig(BaseModel):
    """Gateway-level failover settings."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum backend attempts per request.",
    )
    backoff_base_ms: int = Field(
        default=500,
        ge=0,
        le=10000,
        description="Base exponential backoff delay in milliseconds.",
    )


# --------------------------------------------------------------------------
# Provider quota + saturation policy (OMN-16891)
# --------------------------------------------------------------------------


class EnumQuotaDisposition(StrEnum):
    """What to do with the tier that produced a quota response."""

    RETRYABLE = "retryable"
    """Transient throttle; the tier may retry, then escalate normally."""

    DISABLE_UNTIL_RESET = "disable_until_reset"
    """A periodic cap is spent. Disable until the provider's stated reset."""

    DISABLE_UNTIL_BILLING = "disable_until_billing"
    """No balance/package. No reset is coming — alert, never retry."""


class ModelQuotaCodeRule(BaseModel):
    """One provider error code and the disposition it maps to."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    code: str = Field(..., description="Provider-native error code, as a string.")
    disposition: EnumQuotaDisposition = Field(...)
    reset_from: str | None = Field(
        default=None,
        description=(
            "How to source the reset instant. 'message_reset_timestamp' reads an "
            "absolute stamp the provider states inline (z.ai); "
            "'message_retry_delay' reads a relative 'retry in <N>s' delay "
            "(Gemini, OMN-16932). Anything else falls back to the cooldown."
        ),
    )
    fallback_cooldown_seconds: int = Field(
        default=0,
        ge=0,
        description="Cooldown applied when a declared reset cannot be parsed.",
    )
    alert: bool = Field(
        default=False,
        description="Whether this condition needs an operator, not a retry.",
    )
    alert_hint: str | None = Field(
        default=None,
        description=(
            "Operator-facing cause for this code, declared in the contract and "
            "appended verbatim to the verdict reason. Exists because a "
            "provider's own error text can name the wrong cause: z.ai 1113 "
            "reads 'Insufficient balance' but on a Coding-Plan key it means the "
            "request hit the pay-as-you-go surface (OMN-6790)."
        ),
    )


class ModelQuotaProviderRule(BaseModel):
    """Quota rules for one provider, matched by endpoint host."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    provider_id: str = Field(...)
    match_endpoint_host: str = Field(...)
    codes: tuple[ModelQuotaCodeRule, ...] = Field(default_factory=tuple)
    required_path_prefix: str | None = Field(
        default=None,
        description=(
            "OMN-6790. When set, EVERY backend whose endpoint host matches "
            "``match_endpoint_host`` must carry an endpoint_url whose path "
            "starts with this prefix, or the config fails to load. Exists "
            "because one provider host can serve two different PRODUCTS on "
            "two path prefixes, and presenting our key to the wrong one is "
            "refused with an error that names the wrong cause (z.ai 429 code "
            "1113 'Insufficient balance' on /api/paas/v4 when the account "
            "holds a Coding Plan served only at /api/coding/paas/v4). The "
            "committed contract is already correct; this field makes a WRONG "
            "one un-loadable on the host that has it, which is the only place "
            "a stale installed build or a bad overlay can be caught."
        ),
    )
    required_path_prefix_hint: str | None = Field(
        default=None,
        description=(
            "Operator-facing explanation appended verbatim to the "
            "``required_path_prefix`` load failure, so the reader is told the "
            "cause instead of being sent to the provider's billing page."
        ),
    )


class ModelProviderQuotaPolicy(BaseModel):
    """Contract-declared reading of each provider's 429 responses.

    A 429 is not one failure class: a periodic cap, a billing gap, and an
    ordinary throttle all share the status line but need opposite handling.
    Declaring the mapping here keeps it swappable by overlay (OMN-13215).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: str = Field(...)
    default_disposition: EnumQuotaDisposition = Field(
        default=EnumQuotaDisposition.RETRYABLE,
        description="Applied when a provider match yields no explicit code entry.",
    )
    providers: tuple[ModelQuotaProviderRule, ...] = Field(default_factory=tuple)


class ModelTierSaturationRule(BaseModel):
    """Bounded-wait budget for one tier."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tier: str = Field(...)
    max_wait_ms: int = Field(..., ge=0)
    poll_interval_ms: int = Field(default=1000, ge=1)


class ModelSaturationPolicy(BaseModel):
    """Bounded-wait-then-escalate budgets per tier.

    Never queue on owned capacity indefinitely, and never skip to a metered
    tier while owned capacity is idle. A tier with no rule escalates
    immediately — there is no owned capacity worth waiting for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: str = Field(...)
    default_max_wait_ms: int = Field(default=0, ge=0)
    tiers: tuple[ModelTierSaturationRule, ...] = Field(default_factory=tuple)


class ModelBifrostDelegationConfig(BaseModel):
    """Bifrost delegation gateway configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    config_version: str = Field(..., description="Semver config version.")
    schema_version: str = Field(
        ..., description="Schema identifier for this config format."
    )
    backends: tuple[ModelDelegationBackendConfig, ...] = Field(
        ...,
        min_length=1,
        description="Backend definitions with deployed endpoint URLs.",
    )
    routing_rules: tuple[ModelDelegationRoutingRule, ...] = Field(
        ...,
        min_length=1,
        description="Routing rules evaluated in ascending priority order.",
    )
    default_backends: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Fallback backend IDs when no routing rule matches.",
    )
    circuit_breaker: ModelDelegationCircuitBreakerConfig = Field(
        default_factory=ModelDelegationCircuitBreakerConfig,
        description="Circuit breaker settings applying to all backends.",
    )
    failover: ModelDelegationFailoverConfig = Field(
        default_factory=ModelDelegationFailoverConfig,
        description="Gateway-level failover settings.",
    )
    shadow_mode: ModelDelegationShadowConfig = Field(
        default_factory=ModelDelegationShadowConfig,
        description="Shadow mode configuration for delegation A/B testing.",
    )
    # OMN-16891: both blocks are OPTIONAL so an overlay or a test fixture that
    # omits them still validates, but the loaders that consume them fail loud
    # when they are absent from the committed contract (Rule 8) rather than
    # silently defaulting to "every 429 is retryable" / "never wait".
    provider_quota_policy: ModelProviderQuotaPolicy | None = Field(
        default=None,
        description="How each provider's 429 responses are classified.",
    )
    saturation_policy: ModelSaturationPolicy | None = Field(
        default=None,
        description="Per-tier bounded-wait budgets before escalating.",
    )


__all__: list[str] = [
    "EnumQuotaDisposition",
    "ModelBifrostDelegationConfig",
    "ModelDelegationBackendConfig",
    "ModelDelegationCircuitBreakerConfig",
    "ModelDelegationFailoverConfig",
    "ModelDelegationFallbackPolicy",
    "ModelDelegationRoutingRule",
    "ModelDelegationShadowConfig",
    "ModelProviderQuotaPolicy",
    "ModelQuotaCodeRule",
    "ModelQuotaProviderRule",
    "ModelSaturationPolicy",
    "ModelTierSaturationRule",
]
