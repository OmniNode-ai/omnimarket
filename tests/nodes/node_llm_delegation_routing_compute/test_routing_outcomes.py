# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing outcomes, eligibility boundaries and refusal through the handler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from omnimarket.nodes.node_llm_delegation_routing_compute.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_input import (
    DegradationEntry,
    HealthEntry,
    ModelDelegationRoutingInput,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        assert tz is UTC
        return NOW


@pytest.fixture
def routing_input(monkeypatch: pytest.MonkeyPatch) -> ModelDelegationRoutingInput:
    monkeypatch.setattr(routing, "datetime", FixedDatetime)
    profiles = {
        name: ModelDelegationModelProfile(
            model_id=name,
            endpoint_env=f"LLM_{name.upper()}_URL",
            provider="local" if tier == "local" else "anthropic",
            tier=tier,
            cost_per_1m_input=Decimal("0") if tier == "local" else Decimal("3"),
            cost_per_1m_output=Decimal("0") if tier == "local" else Decimal("15"),
            cost_basis=(
                EnumCostBasis.ZERO_MARGINAL_API_COST
                if tier == "local"
                else EnumCostBasis.CLOUD_API_COST
            ),
            max_context=context,
        )
        for name, tier, context in (
            ("first", "local", 10),
            ("second", "local", 10),
            ("backup", "frontier", 100),
        )
    }
    return ModelDelegationRoutingInput(
        request=ModelLlmDelegationRequest(
            task_type="changelog", prompt_hash="fixture", prompt="short prompt"
        ),
        policy=ModelDelegationRoutingPolicy(
            version="0.1.0",
            pricing_manifest_version="0.1.0",
            model_profiles=profiles,
            task_policies={
                "changelog": ModelDelegationTaskPolicy(
                    task_type="changelog",
                    preferred_models=["first", "second"],
                    fallback="backup",
                    max_tokens=1,
                    temperature=0.3,
                )
            },
        ),
        degradation_state={},
        health_state={},
    )


def _degradation(expires_at: datetime) -> DegradationEntry:
    return DegradationEntry(expires_at=expires_at, reason="quality gate failure")


def _health(*, healthy: bool = True, capacity: bool = True) -> HealthEntry:
    return HealthEntry(healthy=healthy, has_capacity=capacity, checked_at=NOW)


@pytest.mark.parametrize("explicit_health", [False, True])
async def test_first_preferred_model_passes_all_gates(
    routing_input: ModelDelegationRoutingInput, explicit_health: bool
) -> None:
    if explicit_health:
        routing_input.health_state["LLM_FIRST_URL"] = _health()
    result = await routing.HandlerDelegationRouting().handle(routing_input)
    assert result.selection.model_id == "first"
    assert result.selection.tier == "local"
    assert result.selection.endpoint_env == "LLM_FIRST_URL"
    assert result.selection.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST
    assert result.selection.selection_reason == "preferred model selected"
    assert result.selection.skipped_models == ()


@pytest.mark.parametrize("seconds_since_expiry", [0, 1])
async def test_degradation_expiring_at_now_is_eligible(
    routing_input: ModelDelegationRoutingInput, seconds_since_expiry: int
) -> None:
    routing_input.degradation_state[("changelog", "first")] = _degradation(
        NOW - timedelta(seconds=seconds_since_expiry)
    )
    result = await routing.HandlerDelegationRouting().handle(routing_input)
    assert result.selection.model_id == "first"
    assert result.selection.skipped_models == ()


@pytest.mark.parametrize("cause", ["degraded", "unhealthy", "capacity", "missing"])
async def test_skip_advances_to_next_preferred_model_with_audit_reason(
    routing_input: ModelDelegationRoutingInput, cause: str
) -> None:
    if cause == "degraded":
        expiry = NOW + timedelta(seconds=1)
        routing_input.degradation_state[("changelog", "first")] = _degradation(expiry)
        reason = f"degraded until {expiry.isoformat()}: quality gate failure"
    elif cause == "unhealthy":
        routing_input.health_state["LLM_FIRST_URL"] = _health(healthy=False)
        reason = f"endpoint 'LLM_FIRST_URL' reported unhealthy at {NOW.isoformat()}"
    elif cause == "capacity":
        routing_input.health_state["LLM_FIRST_URL"] = _health(capacity=False)
        reason = f"endpoint 'LLM_FIRST_URL' has no capacity (rate-limited) at {NOW.isoformat()}"
    else:
        # Dictionaries remain mutable after cross-reference validation. Exercise
        # the handler's defensive behavior for a subsequently damaged snapshot.
        del routing_input.policy.model_profiles["first"]
        reason = "model_id not found in policy model_profiles"
    result = await routing.HandlerDelegationRouting().handle(routing_input)
    assert result.selection.model_id == "second"
    assert [(s.model_id, s.skip_reason) for s in result.selection.skipped_models] == [
        ("first", reason)
    ]


@pytest.mark.parametrize("characters", [0, 1, 3, 4, 31, 32, 35, 36, 320])
async def test_context_margin_accepts_equality_and_falls_back_above_it(
    routing_input: ModelDelegationRoutingInput, characters: int
) -> None:
    routing_input = routing_input.model_copy(
        update={
            "request": routing_input.request.model_copy(
                update={"prompt": "x" * characters}
            )
        }
    )
    result = await routing.HandlerDelegationRouting().handle(routing_input)
    tokens = max(1, characters // 4)
    if characters <= 35:
        assert result.selection.model_id == "first"
        assert result.selection.skipped_models == ()
    else:
        assert result.selection.model_id == "backup"
        assert result.selection.tier == "frontier"
        assert result.selection.endpoint_env == "LLM_BACKUP_URL"
        assert result.selection.cost_basis == EnumCostBasis.CLOUD_API_COST
        assert result.selection.selection_reason == (
            "fallback used: all preferred models were skipped"
        )
        assert [s.model_id for s in result.selection.skipped_models] == [
            "first",
            "second",
        ]
        assert all(
            s.skip_reason
            == f"estimated_tokens={tokens} exceeds 80% of max_context=10 (8)"
            for s in result.selection.skipped_models
        )


async def test_fallback_preserves_skip_order_and_reason_priority(
    routing_input: ModelDelegationRoutingInput,
) -> None:
    expiry = NOW + timedelta(seconds=1)
    routing_input.degradation_state[("changelog", "first")] = _degradation(expiry)
    # Degradation is checked first, so the first endpoint's health does not win.
    routing_input.health_state["LLM_FIRST_URL"] = _health(healthy=False)
    routing_input.health_state["LLM_SECOND_URL"] = _health(capacity=False)
    routing_input.health_state["LLM_BACKUP_URL"] = _health()
    result = await routing.HandlerDelegationRouting().handle(routing_input)
    assert result.selection.model_id == "backup"
    assert [(s.model_id, s.skip_reason) for s in result.selection.skipped_models] == [
        ("first", f"degraded until {expiry.isoformat()}: quality gate failure"),
        (
            "second",
            f"endpoint 'LLM_SECOND_URL' has no capacity (rate-limited) at {NOW.isoformat()}",
        ),
    ]


@pytest.mark.parametrize("cause", ["missing", "degraded", "unhealthy", "capacity"])
async def test_unusable_fallback_refuses_instead_of_selecting_another_model(
    routing_input: ModelDelegationRoutingInput, cause: str
) -> None:
    routing_input.degradation_state.update(
        {
            ("changelog", name): _degradation(NOW + timedelta(seconds=1))
            for name in ("first", "second")
        }
    )
    if cause == "missing":
        # Damage the validated snapshot to reach the defensive missing fallback.
        del routing_input.policy.model_profiles["backup"]
        message = "Fallback model 'backup' not found in policy model_profiles"
    elif cause == "degraded":
        routing_input.degradation_state[("changelog", "backup")] = _degradation(
            NOW + timedelta(seconds=1)
        )
        message = "Fallback model 'backup' is also degraded"
    else:
        routing_input.health_state["LLM_BACKUP_URL"] = _health(
            healthy=cause != "unhealthy", capacity=cause != "capacity"
        )
        message = "Fallback model 'backup' is also unhealthy"
    with pytest.raises(ValueError, match=message):
        await routing.HandlerDelegationRouting().handle(routing_input)


@pytest.mark.parametrize(
    ("tier", "skip_first", "expected"),
    [
        ("local", False, "first"),
        ("local", True, "second"),
        ("frontier", False, "backup"),
    ],
)
async def test_tier_override_uses_declaration_order_within_the_tier(
    routing_input: ModelDelegationRoutingInput,
    tier: str,
    skip_first: bool,
    expected: str,
) -> None:
    routing_input = routing_input.model_copy(
        update={
            "request": routing_input.request.model_copy(update={"required_tier": tier})
        }
    )
    if skip_first:
        routing_input.health_state["LLM_FIRST_URL"] = _health(healthy=False)
    result = await routing.HandlerDelegationRouting().handle(routing_input)
    assert result.selection.model_id == expected
    assert result.selection.tier == tier
    assert result.selection.selection_reason == (
        f"tier-override={tier!r}: first eligible model in tier"
    )
    assert [s.model_id for s in result.selection.skipped_models] == (
        ["first"] if skip_first else []
    )


@pytest.mark.parametrize("tier", ["absent", "local"])
async def test_tier_override_refuses_without_cross_tier_fallback(
    routing_input: ModelDelegationRoutingInput, tier: str
) -> None:
    routing_input = routing_input.model_copy(
        update={
            "request": routing_input.request.model_copy(update={"required_tier": tier})
        }
    )
    routing_input.health_state.update(
        {f"LLM_{name}_URL": _health(healthy=False) for name in ("FIRST", "SECOND")}
    )
    message = (
        "matched no models in the routing policy"
        if tier == "absent"
        else "Cannot fall back across tiers"
    )
    with pytest.raises(ValueError, match=message):
        await routing.HandlerDelegationRouting().handle(routing_input)


async def test_unknown_task_type_refuses_before_model_selection(
    routing_input: ModelDelegationRoutingInput,
) -> None:
    routing_input = routing_input.model_copy(
        update={
            "request": routing_input.request.model_copy(update={"task_type": "unknown"})
        }
    )
    with pytest.raises(
        ValueError, match="No routing policy found for task_type='unknown'"
    ):
        await routing.HandlerDelegationRouting().handle(routing_input)
