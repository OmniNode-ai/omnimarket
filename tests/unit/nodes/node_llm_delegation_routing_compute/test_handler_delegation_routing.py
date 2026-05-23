# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerDelegationRouting — pure compute routing node (OMN-11775).

Covers every routing path:
- Explicit tier override selects first eligible model of that tier
- Happy path: first preferred model selected
- Model skipped due to active degradation
- Model skipped due to context window overflow (>80% of max_context)
- Model skipped due to unhealthy endpoint
- Model skipped due to no capacity (rate-limited)
- All preferred models skipped → fallback used
- Degraded fallback raises ValueError (all options exhausted)
- Unhealthy fallback raises ValueError (all options exhausted)
- Tier override with no matching models raises ValueError
- Tier override with all candidates skipped raises ValueError (no cross-tier fallback)
- Determinism: same inputs always produce same output
- Expired degradation entry is ignored (treated as not degraded)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_request import (
    ModelLlmDelegationRequest,
)
from omnimarket.models.delegation.llm_cost_routing.model_routing_policy import (
    ModelDelegationModelProfile,
    ModelDelegationRoutingPolicy,
    ModelDelegationTaskPolicy,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.handlers.handler_delegation_routing import (
    HandlerDelegationRouting,
    _estimate_tokens,
    _select_model,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_input import (
    DegradationEntry,
    HealthEntry,
    ModelDelegationRoutingInput,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_output import (
    ModelDelegationRoutingOutput,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
_FUTURE = _NOW + timedelta(hours=1)
_PAST = _NOW - timedelta(hours=1)


def _profile(
    model_id: str,
    tier: str = "local",
    endpoint_env: str = "LLM_LOCAL_URL",
    max_context: int = 100_000,
    cost_basis: EnumCostBasis = EnumCostBasis.ZERO_MARGINAL_API_COST,
) -> ModelDelegationModelProfile:
    return ModelDelegationModelProfile(
        model_id=model_id,
        endpoint_env=endpoint_env,
        provider="local",
        tier=tier,
        cost_per_1m_input=Decimal("0"),
        cost_per_1m_output=Decimal("0"),
        cost_basis=cost_basis,
        max_context=max_context,
    )


def _frontier_profile(
    model_id: str, endpoint_env: str = "LLM_FRONTIER_URL"
) -> ModelDelegationModelProfile:
    return ModelDelegationModelProfile(
        model_id=model_id,
        endpoint_env=endpoint_env,
        provider="anthropic",
        tier="frontier",
        cost_per_1m_input=Decimal("3.0"),
        cost_per_1m_output=Decimal("15.0"),
        cost_basis=EnumCostBasis.CLOUD_API_COST,
        max_context=200_000,
    )


def _policy(
    task_type: str = "changelog",
    preferred: list[str] | None = None,
    fallback: str = "model-c",
    extra_profiles: dict[str, ModelDelegationModelProfile] | None = None,
) -> ModelDelegationRoutingPolicy:
    if preferred is None:
        preferred = ["model-a", "model-b"]
    profiles: dict[str, ModelDelegationModelProfile] = {
        "model-a": _profile("model-a", endpoint_env="LLM_A_URL"),
        "model-b": _profile("model-b", endpoint_env="LLM_B_URL"),
        "model-c": _profile("model-c", endpoint_env="LLM_C_URL"),
    }
    if extra_profiles:
        profiles.update(extra_profiles)
    return ModelDelegationRoutingPolicy(
        version="0.1.0",
        pricing_manifest_version="0.1.0",
        task_policies={
            task_type: ModelDelegationTaskPolicy(
                task_type=task_type,
                preferred_models=preferred,
                fallback=fallback,
                max_tokens=1024,
                temperature=0.3,
            )
        },
        model_profiles=profiles,
    )


def _request(
    task_type: str = "changelog",
    prompt: str = "short prompt",
    required_tier: str | None = None,
) -> ModelLlmDelegationRequest:
    return ModelLlmDelegationRequest(
        task_type=task_type,
        prompt_hash="abc123",
        prompt=prompt,
        required_tier=required_tier,
    )


def _input(
    request: ModelLlmDelegationRequest | None = None,
    policy: ModelDelegationRoutingPolicy | None = None,
    degradation_state: dict[tuple[str, str], DegradationEntry] | None = None,
    health_state: dict[str, HealthEntry] | None = None,
) -> ModelDelegationRoutingInput:
    return ModelDelegationRoutingInput(
        request=request or _request(),
        policy=policy or _policy(),
        degradation_state=degradation_state or {},
        health_state=health_state or {},
    )


def _healthy(endpoint_env: str) -> HealthEntry:
    return HealthEntry(healthy=True, has_capacity=True, checked_at=_NOW)


def _unhealthy(endpoint_env: str) -> HealthEntry:
    return HealthEntry(healthy=False, has_capacity=True, checked_at=_NOW)


def _no_capacity(endpoint_env: str) -> HealthEntry:
    return HealthEntry(healthy=True, has_capacity=False, checked_at=_NOW)


def _degradation(
    expires_at: datetime, reason: str = "quality gate failure"
) -> DegradationEntry:
    return DegradationEntry(expires_at=expires_at, reason=reason)


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def test_estimate_tokens_nonzero() -> None:
    assert _estimate_tokens("") == 1
    assert _estimate_tokens("a" * 400) == 100


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_first_preferred_model_selected() -> None:
    result = _select_model(_input(), _NOW)
    assert result.model_id == "model-a"
    assert result.selection_reason == "preferred model selected"
    assert result.skipped_models == ()


# ---------------------------------------------------------------------------
# Degradation skip
# ---------------------------------------------------------------------------


def test_active_degradation_skips_model() -> None:
    degradation = {("changelog", "model-a"): _degradation(_FUTURE)}
    result = _select_model(_input(degradation_state=degradation), _NOW)
    assert result.model_id == "model-b"
    assert len(result.skipped_models) == 1
    assert result.skipped_models[0].model_id == "model-a"
    assert "degraded until" in result.skipped_models[0].skip_reason


def test_expired_degradation_is_ignored() -> None:
    # Entry exists but expires_at is in the past — should NOT be skipped
    degradation = {("changelog", "model-a"): _degradation(_PAST)}
    result = _select_model(_input(degradation_state=degradation), _NOW)
    assert result.model_id == "model-a"
    assert result.skipped_models == ()


# ---------------------------------------------------------------------------
# Context overflow skip
# ---------------------------------------------------------------------------


def test_context_overflow_skips_model() -> None:
    # model-a has max_context=100_000; 80% = 80_000 tokens.
    # model-b is given max_context=200_000 so it can handle the long prompt.
    # _estimate_tokens uses integer division (len // 4), so 80_001 * 4 = 320_004
    # chars produces estimated_tokens=80_001 which exceeds 80_000 → model-a skipped.
    large_b = _profile("model-b", endpoint_env="LLM_B_URL", max_context=200_000)
    pol = ModelDelegationRoutingPolicy(
        version="0.1.0",
        pricing_manifest_version="0.1.0",
        task_policies={
            "changelog": ModelDelegationTaskPolicy(
                task_type="changelog",
                preferred_models=["model-a", "model-b"],
                fallback="model-c",
                max_tokens=1024,
                temperature=0.3,
            )
        },
        model_profiles={
            "model-a": _profile("model-a", endpoint_env="LLM_A_URL"),
            "model-b": large_b,
            "model-c": _profile("model-c", endpoint_env="LLM_C_URL"),
        },
    )
    long_prompt = "x" * 320_004
    result = _select_model(
        _input(request=_request(prompt=long_prompt), policy=pol), _NOW
    )
    assert result.model_id == "model-b"
    assert any("estimated_tokens" in s.skip_reason for s in result.skipped_models)
    assert result.skipped_models[0].model_id == "model-a"


def test_context_exactly_at_limit_is_not_skipped() -> None:
    # Exactly at 80% threshold should NOT be skipped (> not >=)
    # 80% of 100_000 = 80_000 tokens → 80_000 * 4 = 320_000 chars
    prompt = "x" * 320_000
    result = _select_model(_input(request=_request(prompt=prompt)), _NOW)
    assert result.model_id == "model-a"


# ---------------------------------------------------------------------------
# Health failure skip
# ---------------------------------------------------------------------------


def test_unhealthy_endpoint_skips_model() -> None:
    health = {"LLM_A_URL": _unhealthy("LLM_A_URL")}
    result = _select_model(_input(health_state=health), _NOW)
    assert result.model_id == "model-b"
    assert any("unhealthy" in s.skip_reason for s in result.skipped_models)


def test_no_capacity_skips_model() -> None:
    health = {"LLM_A_URL": _no_capacity("LLM_A_URL")}
    result = _select_model(_input(health_state=health), _NOW)
    assert result.model_id == "model-b"
    assert any("no capacity" in s.skip_reason for s in result.skipped_models)


# ---------------------------------------------------------------------------
# Fallback path
# ---------------------------------------------------------------------------


def test_all_preferred_skipped_uses_fallback() -> None:
    degradation = {
        ("changelog", "model-a"): _degradation(_FUTURE),
        ("changelog", "model-b"): _degradation(_FUTURE),
    }
    result = _select_model(_input(degradation_state=degradation), _NOW)
    assert result.model_id == "model-c"
    assert result.selection_reason == "fallback used: all preferred models were skipped"
    assert len(result.skipped_models) == 2


def test_degraded_fallback_raises() -> None:
    degradation = {
        ("changelog", "model-a"): _degradation(_FUTURE),
        ("changelog", "model-b"): _degradation(_FUTURE),
        ("changelog", "model-c"): _degradation(_FUTURE),
    }
    with pytest.raises(ValueError, match=r"Fallback model.*also degraded"):
        _select_model(_input(degradation_state=degradation), _NOW)


def test_unhealthy_fallback_raises() -> None:
    degradation = {
        ("changelog", "model-a"): _degradation(_FUTURE),
        ("changelog", "model-b"): _degradation(_FUTURE),
    }
    health = {"LLM_C_URL": _unhealthy("LLM_C_URL")}
    with pytest.raises(ValueError, match=r"Fallback model.*also unhealthy"):
        _select_model(_input(degradation_state=degradation, health_state=health), _NOW)


# ---------------------------------------------------------------------------
# Tier override
# ---------------------------------------------------------------------------


def test_tier_override_selects_first_eligible_model_of_tier() -> None:
    frontier = _frontier_profile("frontier-model")
    pol = _policy(
        extra_profiles={"frontier-model": frontier},
    )
    req = _request(required_tier="frontier")
    result = _select_model(_input(request=req, policy=pol), _NOW)
    assert result.model_id == "frontier-model"
    assert result.tier == "frontier"
    assert "tier-override" in result.selection_reason


def test_tier_override_no_matching_tier_raises() -> None:
    req = _request(required_tier="premium")
    with pytest.raises(ValueError, match=r"required_tier=.*matched no models"):
        _select_model(_input(request=req), _NOW)


def test_tier_override_all_candidates_skipped_raises_no_cross_tier_fallback() -> None:
    frontier = _frontier_profile("frontier-model", endpoint_env="LLM_FRONTIER_URL")
    pol = _policy(extra_profiles={"frontier-model": frontier})
    req = _request(required_tier="frontier")
    # Degrade the only frontier model
    degradation = {("changelog", "frontier-model"): _degradation(_FUTURE)}
    with pytest.raises(ValueError, match=r"required_tier=.*all.*candidate.*skipped"):
        _select_model(
            _input(request=req, policy=pol, degradation_state=degradation), _NOW
        )


# ---------------------------------------------------------------------------
# Multiple skip reasons accumulate in skipped_models
# ---------------------------------------------------------------------------


def test_skipped_models_audit_trail_is_complete() -> None:
    degradation = {("changelog", "model-a"): _degradation(_FUTURE)}
    health = {"LLM_B_URL": _unhealthy("LLM_B_URL")}
    result = _select_model(
        _input(degradation_state=degradation, health_state=health), _NOW
    )
    assert result.model_id == "model-c"
    skipped_ids = [s.model_id for s in result.skipped_models]
    assert "model-a" in skipped_ids
    assert "model-b" in skipped_ids


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_same_inputs_same_output() -> None:
    inp = _input()
    r1 = _select_model(inp, _NOW)
    r2 = _select_model(inp, _NOW)
    assert r1 == r2


def test_deterministic_with_multiple_skips() -> None:
    degradation = {("changelog", "model-a"): _degradation(_FUTURE)}
    health = {"LLM_B_URL": _unhealthy("LLM_B_URL")}
    inp = _input(degradation_state=degradation, health_state=health)
    r1 = _select_model(inp, _NOW)
    r2 = _select_model(inp, _NOW)
    assert r1 == r2


# ---------------------------------------------------------------------------
# Unknown task_type raises
# ---------------------------------------------------------------------------


def test_unknown_task_type_raises() -> None:
    req = _request(task_type="nonexistent_task")
    with pytest.raises(ValueError, match="No routing policy found for task_type"):
        _select_model(_input(request=req), _NOW)


# ---------------------------------------------------------------------------
# Handler async interface
# ---------------------------------------------------------------------------


def test_handler_returns_routing_output() -> None:
    handler = HandlerDelegationRouting()
    inp = _input()
    result = asyncio.get_event_loop().run_until_complete(handler.handle(uuid4(), inp))
    assert isinstance(result, ModelDelegationRoutingOutput)
    assert result.selection.model_id == "model-a"
