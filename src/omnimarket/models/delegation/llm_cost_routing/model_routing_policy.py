# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed routing policy models for LLM delegation (OMN-11774).

Routing policies are the source of truth for which model to use for a given
task type, including preferred models, fallbacks, and token/temperature limits.

Cross-reference validation is strict: unknown model IDs, providers, fallbacks,
or context window overflows are hard rejections at construction time.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from omnimarket.enums.enum_cost_basis import EnumCostBasis

# Providers declared in the model registry — any other value is rejected.
_KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {"local", "anthropic", "openrouter", "google", "openai"}
)


class ModelDelegationModelProfile(BaseModel):
    """Profile for a delegatable LLM model as declared in a routing policy.

    Unlike ModelLlmModelProfile (registry), this model is embedded directly
    in routing policy YAML so consumers can declare all model facts inline
    without loading the separate registry file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    model_id: str
    endpoint_env: str
    """Name of the env var that resolves to the endpoint URL. Never a raw URL."""

    provider: str
    tier: str
    """Logical tier label: 'local', 'free', 'paid', 'premium'."""

    cost_per_1m_input: Decimal
    cost_per_1m_output: Decimal
    cost_basis: EnumCostBasis
    max_context: int

    model_name: str | None = None
    """Provider-specific model identifier (e.g. OpenRouter model slug)."""

    requires_api_key_env: str | None = None
    """Name of env var for API key, if required."""

    @field_validator("cost_per_1m_input", "cost_per_1m_output", mode="before")
    @classmethod
    def coerce_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))

    @field_validator("max_context")
    @classmethod
    def max_context_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"max_context must be > 0, got {v}")
        return v

    @field_validator("endpoint_env")
    @classmethod
    def endpoint_env_not_url(cls, v: str) -> str:
        if v.startswith("http://") or v.startswith("https://"):
            raise ValueError(f"endpoint_env must be an env var name, not a URL: {v!r}")
        return v

    @field_validator("provider")
    @classmethod
    def provider_known(cls, v: str) -> str:
        if v not in _KNOWN_PROVIDERS:
            raise ValueError(
                f"Unknown provider {v!r}. Known providers: {sorted(_KNOWN_PROVIDERS)}"
            )
        return v


class ModelDelegationTaskPolicy(BaseModel):
    """Policy for a single task type: which models to prefer and how to call them."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_type: str
    preferred_models: list[str]
    """Ordered list of model_id references. Tried left-to-right."""

    fallback: str
    """model_id reference used when all preferred models fail."""

    max_tokens: int
    temperature: float

    @field_validator("preferred_models")
    @classmethod
    def preferred_models_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("preferred_models must not be empty")
        return v

    @field_validator("max_tokens")
    @classmethod
    def max_tokens_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"max_tokens must be > 0, got {v}")
        return v

    @field_validator("temperature")
    @classmethod
    def temperature_range(cls, v: float) -> float:
        if not (0.0 <= v <= 2.0):
            raise ValueError(f"temperature must be in [0.0, 2.0], got {v}")
        return v


class ModelDelegationRoutingPolicy(BaseModel):
    """Top-level routing policy with strict cross-reference validation.

    All model_id references in task_policies must resolve to entries in
    model_profiles. Violations are hard rejections at construction time —
    there are no implicit defaults or silent fallbacks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    version: str
    pricing_manifest_version: str
    task_policies: dict[str, ModelDelegationTaskPolicy]
    model_profiles: dict[str, ModelDelegationModelProfile]

    @model_validator(mode="after")
    def validate_cross_references(self) -> Self:
        known_ids = set(self.model_profiles.keys())

        for task_type, policy in self.task_policies.items():
            # Validate preferred_models references
            unknown_preferred = [
                m for m in policy.preferred_models if m not in known_ids
            ]
            if unknown_preferred:
                raise ValueError(
                    f"task_policies[{task_type!r}].preferred_models references "
                    f"unknown model IDs: {unknown_preferred}. "
                    f"Known profiles: {sorted(known_ids)}"
                )

            # Validate fallback reference
            if policy.fallback not in known_ids:
                raise ValueError(
                    f"task_policies[{task_type!r}].fallback references "
                    f"unknown model ID {policy.fallback!r}. "
                    f"Known profiles: {sorted(known_ids)}"
                )

            # Validate max_tokens does not exceed any preferred model's context window
            for model_id in policy.preferred_models:
                profile = self.model_profiles[model_id]
                if policy.max_tokens > profile.max_context:
                    raise ValueError(
                        f"task_policies[{task_type!r}].max_tokens={policy.max_tokens} "
                        f"exceeds {model_id!r} context window ({profile.max_context}). "
                        "Reduce max_tokens or remove the model from preferred_models."
                    )

            # Also validate fallback context window
            fallback_profile = self.model_profiles[policy.fallback]
            if policy.max_tokens > fallback_profile.max_context:
                raise ValueError(
                    f"task_policies[{task_type!r}].max_tokens={policy.max_tokens} "
                    f"exceeds fallback model {policy.fallback!r} context window "
                    f"({fallback_profile.max_context})."
                )

        # Validate all model profiles have known providers and non-UNKNOWN pricing
        for model_id, profile in self.model_profiles.items():
            if profile.provider not in _KNOWN_PROVIDERS:
                raise ValueError(
                    f"model_profiles[{model_id!r}].provider {profile.provider!r} "
                    f"is not in the known provider set: {sorted(_KNOWN_PROVIDERS)}"
                )
            # UNKNOWN cost_basis requires explicit acknowledgement — no silent gaps
            if profile.cost_basis == EnumCostBasis.UNKNOWN:
                raise ValueError(
                    f"model_profiles[{model_id!r}].cost_basis is UNKNOWN. "
                    "Routing policies must declare an explicit cost basis. "
                    "Use ZERO_MARGINAL_API_COST for local/free models."
                )

        return self
