# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for LLM delegation routing policy models (OMN-11774).

Every rejection path in ModelDelegationRoutingPolicy.validate_cross_references
has a dedicated test. Valid construction is also exercised.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.models.delegation.llm_cost_routing import (
    ModelDelegationModelProfile,
    ModelDelegationRoutingPolicy,
    ModelDelegationTaskPolicy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(
    model_id: str = "local-model",
    provider: str = "local",
    max_context: int = 32000,
    cost_basis: EnumCostBasis = EnumCostBasis.ZERO_MARGINAL_API_COST,
    tier: str = "local",
) -> ModelDelegationModelProfile:
    return ModelDelegationModelProfile(
        model_id=model_id,
        endpoint_env="LLM_TEST_URL",
        provider=provider,
        tier=tier,
        cost_per_1m_input="0.00",
        cost_per_1m_output="0.00",
        cost_basis=cost_basis,
        max_context=max_context,
    )


def _make_task_policy(
    task_type: str = "code_review",
    preferred_models: list[str] | None = None,
    fallback: str = "local-model",
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> ModelDelegationTaskPolicy:
    return ModelDelegationTaskPolicy(
        task_type=task_type,
        preferred_models=preferred_models or ["local-model"],
        fallback=fallback,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _make_routing_policy(
    profiles: dict[str, ModelDelegationModelProfile] | None = None,
    task_policies: dict[str, ModelDelegationTaskPolicy] | None = None,
) -> ModelDelegationRoutingPolicy:
    if profiles is None:
        profiles = {"local-model": _make_profile()}
    if task_policies is None:
        task_policies = {"code_review": _make_task_policy()}
    return ModelDelegationRoutingPolicy(
        version="1.0.0",
        pricing_manifest_version="2026-05-23-test",
        task_policies=task_policies,
        model_profiles=profiles,
    )


# ---------------------------------------------------------------------------
# ModelDelegationModelProfile validation
# ---------------------------------------------------------------------------


class TestModelDelegationModelProfile:
    def test_valid_construction(self) -> None:
        profile = _make_profile()
        assert profile.model_id == "local-model"
        assert profile.max_context == 32000
        assert isinstance(profile.cost_per_1m_input, Decimal)

    def test_pricing_coerced_to_decimal(self) -> None:
        profile = ModelDelegationModelProfile(
            model_id="m",
            endpoint_env="URL_ENV",
            provider="local",
            tier="local",
            cost_per_1m_input="3.50",
            cost_per_1m_output="12.00",
            cost_basis=EnumCostBasis.CLOUD_API_COST,
            max_context=8000,
        )
        assert profile.cost_per_1m_input == Decimal("3.50")
        assert profile.cost_per_1m_output == Decimal("12.00")

    def test_max_context_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_context must be > 0"):
            ModelDelegationModelProfile(
                model_id="m",
                endpoint_env="URL_ENV",
                provider="local",
                tier="local",
                cost_per_1m_input="0.00",
                cost_per_1m_output="0.00",
                cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
                max_context=0,
            )

    def test_max_context_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_context must be > 0"):
            ModelDelegationModelProfile(
                model_id="m",
                endpoint_env="URL_ENV",
                provider="local",
                tier="local",
                cost_per_1m_input="0.00",
                cost_per_1m_output="0.00",
                cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
                max_context=-1,
            )

    def test_endpoint_env_as_url_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="endpoint_env must be an env var name"
        ):
            ModelDelegationModelProfile(
                model_id="m",
                endpoint_env="http://localhost:8000",  # test-literal-ok: deliberately bad value to test URL rejection
                provider="local",
                tier="local",
                cost_per_1m_input="0.00",
                cost_per_1m_output="0.00",
                cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
                max_context=8000,
            )

    def test_https_endpoint_env_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="endpoint_env must be an env var name"
        ):
            ModelDelegationModelProfile(
                model_id="m",
                endpoint_env="https://api.anthropic.com",
                provider="local",
                tier="local",
                cost_per_1m_input="0.00",
                cost_per_1m_output="0.00",
                cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
                max_context=8000,
            )

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unknown provider"):
            ModelDelegationModelProfile(
                model_id="m",
                endpoint_env="URL_ENV",
                provider="mystery_cloud",
                tier="free",
                cost_per_1m_input="0.00",
                cost_per_1m_output="0.00",
                cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
                max_context=8000,
            )

    def test_frozen(self) -> None:
        profile = _make_profile()
        with pytest.raises((ValidationError, TypeError)):
            profile.model_id = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelDelegationModelProfile(
                model_id="m",
                endpoint_env="URL_ENV",
                provider="local",
                tier="local",
                cost_per_1m_input="0.00",
                cost_per_1m_output="0.00",
                cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
                max_context=8000,
                surprise_field="oops",
            )

    def test_optional_fields_default_none(self) -> None:
        profile = _make_profile()
        assert profile.model_name is None
        assert profile.requires_api_key_env is None

    def test_optional_fields_accepted(self) -> None:
        profile = ModelDelegationModelProfile(
            model_id="cloud-model",
            endpoint_env="OPENROUTER_URL",
            provider="openrouter",
            tier="free",
            cost_per_1m_input="0.00",
            cost_per_1m_output="0.00",
            cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
            max_context=131072,
            model_name="meta-llama/llama-3.3-70b-instruct:free",
            requires_api_key_env="OPENROUTER_API_KEY",
        )
        assert profile.model_name == "meta-llama/llama-3.3-70b-instruct:free"
        assert profile.requires_api_key_env == "OPENROUTER_API_KEY"


# ---------------------------------------------------------------------------
# ModelDelegationTaskPolicy validation
# ---------------------------------------------------------------------------


class TestModelDelegationTaskPolicy:
    def test_valid_construction(self) -> None:
        policy = _make_task_policy()
        assert policy.task_type == "code_review"
        assert policy.preferred_models == ["local-model"]
        assert policy.fallback == "local-model"

    def test_empty_preferred_models_rejected(self) -> None:
        with pytest.raises(ValidationError, match="preferred_models must not be empty"):
            ModelDelegationTaskPolicy(
                task_type="code_review",
                preferred_models=[],
                fallback="local-model",
                max_tokens=4096,
                temperature=0.2,
            )

    def test_max_tokens_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_tokens must be > 0"):
            ModelDelegationTaskPolicy(
                task_type="code_review",
                preferred_models=["local-model"],
                fallback="local-model",
                max_tokens=0,
                temperature=0.2,
            )

    def test_max_tokens_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_tokens must be > 0"):
            ModelDelegationTaskPolicy(
                task_type="code_review",
                preferred_models=["local-model"],
                fallback="local-model",
                max_tokens=-100,
                temperature=0.2,
            )

    def test_temperature_too_high_rejected(self) -> None:
        with pytest.raises(ValidationError, match="temperature must be in"):
            ModelDelegationTaskPolicy(
                task_type="code_review",
                preferred_models=["local-model"],
                fallback="local-model",
                max_tokens=4096,
                temperature=2.1,
            )

    def test_temperature_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="temperature must be in"):
            ModelDelegationTaskPolicy(
                task_type="code_review",
                preferred_models=["local-model"],
                fallback="local-model",
                max_tokens=4096,
                temperature=-0.1,
            )

    def test_temperature_boundary_values_accepted(self) -> None:
        p0 = _make_task_policy(temperature=0.0)
        p2 = _make_task_policy(temperature=2.0)
        assert p0.temperature == 0.0
        assert p2.temperature == 2.0

    def test_frozen(self) -> None:
        policy = _make_task_policy()
        with pytest.raises((ValidationError, TypeError)):
            policy.task_type = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelDelegationTaskPolicy(
                task_type="code_review",
                preferred_models=["local-model"],
                fallback="local-model",
                max_tokens=4096,
                temperature=0.2,
                surprise="bad",
            )


# ---------------------------------------------------------------------------
# ModelDelegationRoutingPolicy cross-reference validation
# ---------------------------------------------------------------------------


class TestModelDelegationRoutingPolicyValid:
    def test_valid_single_task(self) -> None:
        policy = _make_routing_policy()
        assert policy.version == "1.0.0"
        assert "code_review" in policy.task_policies
        assert "local-model" in policy.model_profiles

    def test_valid_multiple_preferred_models(self) -> None:
        profiles = {
            "model-a": _make_profile("model-a", max_context=8000),
            "model-b": _make_profile("model-b", max_context=16000),
            "fallback": _make_profile("fallback", max_context=32000),
        }
        task_policies = {
            "summarize": _make_task_policy(
                preferred_models=["model-a", "model-b"],
                fallback="fallback",
                max_tokens=4096,
            )
        }
        policy = _make_routing_policy(profiles=profiles, task_policies=task_policies)
        assert len(policy.task_policies["summarize"].preferred_models) == 2

    def test_valid_multiple_task_types(self) -> None:
        profiles = {
            "local-model": _make_profile("local-model", max_context=32000),
            "cloud-model": _make_profile(
                "cloud-model",
                provider="anthropic",
                max_context=200000,
                cost_basis=EnumCostBasis.CLOUD_API_COST,
                tier="premium",
            ),
        }
        task_policies = {
            "quick_task": _make_task_policy(
                task_type="quick_task",
                preferred_models=["local-model"],
                fallback="local-model",
                max_tokens=1024,
            ),
            "deep_analysis": _make_task_policy(
                task_type="deep_analysis",
                preferred_models=["cloud-model"],
                fallback="cloud-model",
                max_tokens=8192,
            ),
        }
        policy = _make_routing_policy(profiles=profiles, task_policies=task_policies)
        assert len(policy.task_policies) == 2

    def test_frozen(self) -> None:
        policy = _make_routing_policy()
        with pytest.raises((ValidationError, TypeError)):
            policy.version = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelDelegationRoutingPolicy(
                version="1.0.0",
                pricing_manifest_version="test",
                task_policies={},
                model_profiles={},
                surprise="bad",
            )


class TestModelDelegationRoutingPolicyUnknownPreferred:
    def test_unknown_preferred_model_rejected(self) -> None:
        profiles = {"local-model": _make_profile()}
        task_policies = {
            "code_review": _make_task_policy(
                preferred_models=["local-model", "ghost-model"],
                fallback="local-model",
            )
        }
        with pytest.raises(ValidationError, match=r"unknown model IDs.*ghost-model"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)

    def test_multiple_unknown_preferred_models_all_reported(self) -> None:
        profiles = {"local-model": _make_profile()}
        task_policies = {
            "code_review": _make_task_policy(
                preferred_models=["ghost-a", "ghost-b"],
                fallback="local-model",
            )
        }
        with pytest.raises(ValidationError, match="unknown model IDs"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)


class TestModelDelegationRoutingPolicyUnknownFallback:
    def test_unknown_fallback_rejected(self) -> None:
        profiles = {"local-model": _make_profile()}
        task_policies = {
            "code_review": _make_task_policy(
                preferred_models=["local-model"],
                fallback="nonexistent-fallback",
            )
        }
        with pytest.raises(ValidationError, match="nonexistent-fallback"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)


class TestModelDelegationRoutingPolicyContextWindowOverflow:
    def test_max_tokens_exceeds_preferred_model_context_rejected(self) -> None:
        profiles = {
            "small-model": _make_profile("small-model", max_context=4096),
        }
        task_policies = {
            "code_review": _make_task_policy(
                preferred_models=["small-model"],
                fallback="small-model",
                max_tokens=8192,  # exceeds 4096
            )
        }
        with pytest.raises(ValidationError, match=r"exceeds.*context window"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)

    def test_max_tokens_exactly_at_limit_accepted(self) -> None:
        profiles = {"model": _make_profile("model", max_context=4096)}
        task_policies = {
            "task": _make_task_policy(
                preferred_models=["model"],
                fallback="model",
                max_tokens=4096,
            )
        }
        policy = _make_routing_policy(profiles=profiles, task_policies=task_policies)
        assert policy.task_policies["task"].max_tokens == 4096

    def test_max_tokens_exceeds_fallback_context_rejected(self) -> None:
        profiles = {
            "big-preferred": _make_profile("big-preferred", max_context=32000),
            "small-fallback": _make_profile("small-fallback", max_context=2048),
        }
        task_policies = {
            "code_review": _make_task_policy(
                preferred_models=["big-preferred"],
                fallback="small-fallback",
                max_tokens=8192,  # fits big-preferred but not small-fallback
            )
        }
        with pytest.raises(ValidationError, match="exceeds fallback model"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)

    def test_max_tokens_one_over_context_rejected(self) -> None:
        profiles = {"model": _make_profile("model", max_context=4096)}
        task_policies = {
            "task": _make_task_policy(
                preferred_models=["model"],
                fallback="model",
                max_tokens=4097,
            )
        }
        with pytest.raises(ValidationError, match=r"exceeds.*context window"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)


class TestModelDelegationRoutingPolicyUnknownCostBasis:
    def test_unknown_cost_basis_in_profile_rejected(self) -> None:
        profiles = {
            "mystery-model": _make_profile(
                "mystery-model", cost_basis=EnumCostBasis.UNKNOWN
            )
        }
        task_policies = {
            "code_review": _make_task_policy(
                preferred_models=["mystery-model"],
                fallback="mystery-model",
            )
        }
        with pytest.raises(ValidationError, match="cost_basis is UNKNOWN"):
            _make_routing_policy(profiles=profiles, task_policies=task_policies)
