# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation wire DTOs — canonical source is omnibase_core (OMN-12659).

Shared platform-primitive models re-exported from omnibase_core.
Omnimarket-specific projection models remain local.
"""

from __future__ import annotations

import yaml

# --- Shared models from omnibase_core (canonical source, OMN-12126) ---
from omnibase_core.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
    ModelDelegationBackendConfig,
    ModelDelegationCircuitBreakerConfig,
    ModelDelegationFailoverConfig,
    ModelDelegationFallbackPolicy,
    ModelDelegationRoutingRule,
    ModelDelegationShadowConfig,
)
from omnibase_core.models.delegation.wire.model_budget import (
    EnumBudgetAction,
    ModelBudgetLimits,
)
from omnibase_core.models.delegation.wire.model_delegation_completed import (
    ModelDelegationCompleted,
)
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)
from omnibase_core.models.delegation.wire.model_delegation_result import (
    ModelDelegationResult,
)
from omnibase_core.models.delegation.wire.model_delegation_wire_request import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
    EnumQualityContractMode,
    ModelDelegationRequest,
    validate_acceptance_criteria,
)
from omnibase_core.models.delegation.wire.model_orchestrator_intents import (
    ModelBaselineIntent,
    ModelComplianceLoopResult,
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from omnibase_core.models.delegation.wire.model_quality_gate import (
    EnumQualityGateCategory,
    ModelQualityGateInput,
    ModelQualityGateResult,
)
from omnibase_core.models.delegation.wire.model_routing_config import (
    EnumTierCostType,
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierCost,
    ModelTierModel,
)
from omnibase_core.models.delegation.wire.model_task_delegated_event import (
    TASK_DELEGATED_TOPIC_V1,
    ModelTaskDelegatedEvent,
)

# --- Omnimarket-specific projection models (not in core) ---
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
    ModelProjectionEnvelopeMetadata,
)
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.models.delegation.wire.model_token_limits import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
)


def _parse_tier_cost(raw_cost: object) -> ModelTierCost | None:
    if raw_cost is None:
        return None
    if not isinstance(raw_cost, dict):
        raise ValueError("routing_tiers.yaml tier 'cost' must be a mapping")
    raw_type = raw_cost.get("cost_type")
    if not isinstance(raw_type, str):
        raise ValueError("routing_tiers.yaml tier 'cost.cost_type' must be a string")
    raw_cap = raw_cost.get("monthly_cap_usd")
    return ModelTierCost(
        cost_type=EnumTierCostType(raw_type),
        rate_per_1k_usd=float(raw_cost.get("rate_per_1k_usd", 0.0)),
        monthly_cap_usd=None if raw_cap is None else float(raw_cap),
        overage_rate_per_1k_usd=float(raw_cost.get("overage_rate_per_1k_usd", 0.0)),
    )


def parse_delegation_config_yaml(yaml_text: str) -> ModelDelegationConfig:
    """Parse routing_tiers.yaml into the canonical delegation config DTO."""
    raw = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict):
        raise ValueError(
            "routing_tiers.yaml must be a mapping with a top-level 'tiers' key"
        )

    raw_tiers = raw.get("tiers", [])
    if not isinstance(raw_tiers, list):
        raise ValueError("routing_tiers.yaml 'tiers' must be a list")

    tiers: list[ModelRoutingTier] = []
    for tier_data in raw_tiers:
        if not isinstance(tier_data, dict):
            raise ValueError("routing_tiers.yaml tier entries must be mappings")

        raw_models = tier_data.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError("routing_tiers.yaml tier 'models' must be a list")

        models: list[ModelTierModel] = []
        for model_data in raw_models:
            if not isinstance(model_data, dict):
                raise ValueError("routing_tiers.yaml model entries must be mappings")

            raw_use_for = model_data.get("use_for", [])
            if isinstance(raw_use_for, str):
                use_for = (raw_use_for,)
            elif isinstance(raw_use_for, list):
                use_for = tuple(raw_use_for)
            else:
                raise ValueError(
                    "routing_tiers.yaml model 'use_for' must be a string or list"
                )

            models.append(
                ModelTierModel(
                    id=model_data["id"],
                    backend_ref=model_data["backend_id"],
                    max_context_tokens=model_data["max_context_tokens"],
                    use_for=use_for,
                    fast_path_threshold_tokens=model_data.get(
                        "fast_path_threshold_tokens"
                    ),
                )
            )
        tiers.append(
            ModelRoutingTier(
                name=tier_data["name"],
                models=tuple(models),
                eval_before_accept=tier_data.get("eval_before_accept", False),
                eval_model=tier_data.get("eval_model"),
                max_retries=tier_data.get("max_retries", 0),
                cost_per_1k_tokens=float(tier_data.get("cost_per_1k_tokens", 0.0)),
                cost=_parse_tier_cost(tier_data.get("cost")),
            )
        )
    return ModelDelegationConfig(tiers=tuple(tiers))


__all__: list[str] = [
    "DELEGATION_DEFAULT_MAX_TOKENS",
    "DELEGATION_MAX_TOKENS_HARD_LIMIT",
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "TASK_DELEGATED_TOPIC_V1",
    "EnumBudgetAction",
    "EnumQualityContractMode",
    "EnumQualityGateCategory",
    "EnumTierCostType",
    "ModelBaselineIntent",
    "ModelBifrostDelegationConfig",
    "ModelBudgetLimits",
    "ModelComplianceLoopResult",
    "ModelDelegateSkillAttemptRecord",
    "ModelDelegateSkillRequest",
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
    "ModelDelegateSkillSavingsProjection",
    "ModelDelegateSkillTerminalProjection",
    "ModelDelegationBackendConfig",
    "ModelDelegationCircuitBreakerConfig",
    "ModelDelegationCompleted",
    "ModelDelegationConfig",
    "ModelDelegationEventProjectionRow",
    "ModelDelegationFailed",
    "ModelDelegationFailoverConfig",
    "ModelDelegationFallbackPolicy",
    "ModelDelegationRequest",
    "ModelDelegationResult",
    "ModelDelegationRoutingRule",
    "ModelDelegationShadowConfig",
    "ModelInferenceIntent",
    "ModelInferenceResponseData",
    "ModelProjectionEnvelopeMetadata",
    "ModelQualityGateInput",
    "ModelQualityGateIntent",
    "ModelQualityGateResult",
    "ModelRoutingDecision",
    "ModelRoutingIntent",
    "ModelRoutingTier",
    "ModelTaskDelegatedEvent",
    "ModelTierCost",
    "ModelTierModel",
    "parse_delegation_config_yaml",
    "validate_acceptance_criteria",
]
