# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""LLM delegation routing handler — pure compute, no I/O.

Selects a target LLM model from a routing policy given:
- DelegationRequest (including optional required_tier override)
- ModelDelegationRoutingPolicy (preferred models, fallback, context limits)
- degradation_state: time-bounded degradation entries per (task_type, model_id)
- health_state: live health snapshot per endpoint_env

Routing algorithm (deterministic, stateless):
1. If required_tier is set, filter model_profiles to that tier only and treat
   the first matching non-degraded, non-unhealthy model as selected.
2. Otherwise, try preferred_models in declaration order:
   a. Skip if active degradation entry exists for (task_type, model_id).
   b. Skip if estimated tokens exceed max_context * 0.8.
   c. Skip if health_state marks the endpoint as unhealthy or at-capacity.
3. If all preferred models are skipped, use the fallback model.
4. Never silently degrade to a model that failed quality gates — every skip
   is recorded in skipped_models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_input import (
    DegradationEntry,
    HealthEntry,
    ModelDelegationRoutingInput,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_output import (
    ModelDelegationRoutingOutput,
    ModelSelection,
    SkippedModel,
)

__all__ = ["HandlerDelegationRouting"]

# Fraction of max_context at which we consider a model "too small" for the request.
_CONTEXT_SAFETY_MARGIN = 0.8

# Rough token estimate: 4 chars per token (conservative).
_CHARS_PER_TOKEN = 4


def _estimate_tokens(prompt: str) -> int:
    return max(1, len(prompt) // _CHARS_PER_TOKEN)


def _is_degraded(
    degradation_state: dict[tuple[str, str], DegradationEntry],
    task_type: str,
    model_id: str,
    now: datetime,
) -> tuple[bool, str]:
    entry = degradation_state.get((task_type, model_id))
    if entry is None:
        return False, ""
    if entry.expires_at > now:
        return True, f"degraded until {entry.expires_at.isoformat()}: {entry.reason}"
    return False, ""


def _is_unhealthy(
    health_state: dict[str, HealthEntry],
    endpoint_env: str,
) -> tuple[bool, str]:
    entry = health_state.get(endpoint_env)
    if entry is None:
        return False, ""
    if not entry.healthy:
        return (
            True,
            f"endpoint {endpoint_env!r} reported unhealthy at {entry.checked_at.isoformat()}",
        )
    if not entry.has_capacity:
        return (
            True,
            f"endpoint {endpoint_env!r} has no capacity (rate-limited) at {entry.checked_at.isoformat()}",
        )
    return False, ""


def _select_model(
    input_data: ModelDelegationRoutingInput,
    now: datetime,
) -> ModelSelection:
    request = input_data.request
    policy = input_data.policy
    task_type = request.task_type

    task_policy = policy.task_policies.get(task_type)
    if task_policy is None:
        raise ValueError(
            f"No routing policy found for task_type={task_type!r}. "
            f"Known task types: {sorted(policy.task_policies)}"
        )

    estimated_tokens = _estimate_tokens(request.prompt)
    skipped: list[SkippedModel] = []

    # Build candidate list: preferred models, then fallback
    candidates = list(task_policy.preferred_models)
    fallback_id = task_policy.fallback

    # Determine if required_tier overrides the candidate list
    required_tier = request.required_tier
    if required_tier is not None:
        tier_candidates = [
            mid
            for mid, prof in policy.model_profiles.items()
            if prof.tier == required_tier
        ]
        if not tier_candidates:
            raise ValueError(
                f"required_tier={required_tier!r} matched no models in the routing policy. "
                f"Available tiers: {sorted({p.tier for p in policy.model_profiles.values()})}"
            )
        candidates = tier_candidates
        # Do not apply fallback logic when tier override is active; all tier
        # candidates are tried in policy declaration order, failing hard if none pass.

    for model_id in candidates:
        profile = policy.model_profiles.get(model_id)
        if profile is None:
            skipped.append(
                SkippedModel(
                    model_id=model_id,
                    skip_reason="model_id not found in policy model_profiles",
                )
            )
            continue

        degraded, degrade_reason = _is_degraded(
            input_data.degradation_state, task_type, model_id, now
        )
        if degraded:
            skipped.append(SkippedModel(model_id=model_id, skip_reason=degrade_reason))
            continue

        context_limit = int(profile.max_context * _CONTEXT_SAFETY_MARGIN)
        if estimated_tokens > context_limit:
            skipped.append(
                SkippedModel(
                    model_id=model_id,
                    skip_reason=(
                        f"estimated_tokens={estimated_tokens} exceeds "
                        f"80% of max_context={profile.max_context} ({context_limit})"
                    ),
                )
            )
            continue

        unhealthy, health_reason = _is_unhealthy(
            input_data.health_state, profile.endpoint_env
        )
        if unhealthy:
            skipped.append(SkippedModel(model_id=model_id, skip_reason=health_reason))
            continue

        # This model passes all gates.
        reason = "preferred model selected"
        if required_tier is not None:
            reason = f"tier-override={required_tier!r}: first eligible model in tier"

        return ModelSelection(
            model_id=model_id,
            tier=profile.tier,
            endpoint_env=profile.endpoint_env,
            cost_basis=profile.cost_basis,
            selection_reason=reason,
            skipped_models=tuple(skipped),
        )

    # All preferred (or tier-filtered) candidates exhausted — use fallback.
    if required_tier is not None:
        raise ValueError(
            f"required_tier={required_tier!r}: all {len(candidates)} candidate(s) were skipped. "
            f"Skipped: {[s.model_id for s in skipped]}. Cannot fall back across tiers."
        )

    fallback_profile = policy.model_profiles.get(fallback_id)
    if fallback_profile is None:
        raise ValueError(
            f"Fallback model {fallback_id!r} not found in policy model_profiles. "
            f"All preferred models were skipped."
        )

    # Validate the fallback itself is usable — a fallback that also fails is a hard error,
    # not a silent second fallback.
    degraded, degrade_reason = _is_degraded(
        input_data.degradation_state, task_type, fallback_id, now
    )
    if degraded:
        raise ValueError(
            f"Fallback model {fallback_id!r} is also degraded: {degrade_reason}. "
            "All routing options exhausted."
        )

    unhealthy, health_reason = _is_unhealthy(
        input_data.health_state, fallback_profile.endpoint_env
    )
    if unhealthy:
        raise ValueError(
            f"Fallback model {fallback_id!r} is also unhealthy: {health_reason}. "
            "All routing options exhausted."
        )

    return ModelSelection(
        model_id=fallback_id,
        tier=fallback_profile.tier,
        endpoint_env=fallback_profile.endpoint_env,
        cost_basis=fallback_profile.cost_basis,
        selection_reason="fallback used: all preferred models were skipped",
        skipped_models=tuple(skipped),
    )


class HandlerDelegationRouting:
    """Pure compute handler for LLM delegation model routing.

    Stateless, deterministic. No I/O, no side effects.
    """

    handler_type = "NODE_HANDLER"
    handler_category = "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        input_data: ModelDelegationRoutingInput,
    ) -> ModelDelegationRoutingOutput:
        now = datetime.now(tz=UTC)
        selection = _select_model(input_data, now)
        return ModelDelegationRoutingOutput(selection=selection)
