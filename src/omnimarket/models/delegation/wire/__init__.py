# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation wire DTOs — canonical source is omnibase_compat (OMN-11969).

Shared platform-primitive models re-exported from omnibase_compat.
Omnimarket-specific projection models remain local.
"""

# --- Shared models from omnibase_compat (canonical source) ---

from omnibase_compat.contracts.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
    ModelDelegationBackendConfig,
    ModelDelegationCircuitBreakerConfig,
    ModelDelegationFailoverConfig,
    ModelDelegationFallbackPolicy,
    ModelDelegationRoutingRule,
    ModelDelegationShadowConfig,
)
from omnibase_compat.contracts.delegation.wire.model_budget import (
    EnumBudgetAction,
    ModelBudgetLimits,
)
from omnibase_compat.contracts.delegation.wire.model_delegation_request import (
    MAX_WORDS_PER_SENTENCE_RE,
    SUPPORTED_ACCEPTANCE_CRITERIA,
    EnumQualityContractMode,
    ModelDelegationRequest,
    validate_acceptance_criteria,
)
from omnibase_compat.contracts.delegation.wire.model_delegation_result import (
    ModelDelegationResult,
)
from omnibase_compat.contracts.delegation.wire.model_event_envelope import (
    ModelDelegationEventEnvelope,
)
from omnibase_compat.contracts.delegation.wire.model_orchestrator_intents import (
    ModelBaselineIntent,
    ModelComplianceLoopResult,
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from omnibase_compat.contracts.delegation.wire.model_quality_gate import (
    EnumQualityGateCategory,
    ModelQualityGateInput,
    ModelQualityGateResult,
)
from omnibase_compat.contracts.delegation.wire.model_routing_config import (
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierModel,
)
from omnibase_compat.contracts.delegation.wire.model_task_delegated_event import (
    TASK_DELEGATED_TOPIC_V1,
    ModelTaskDelegatedEvent,
)

# --- Omnimarket-specific projection models (not in compat) ---
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
    ModelProjectionEnvelopeMetadata,
)
from omnimarket.models.delegation.wire.model_token_limits import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
)

__all__: list[str] = [
    "DELEGATION_DEFAULT_MAX_TOKENS",
    "DELEGATION_MAX_TOKENS_HARD_LIMIT",
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "TASK_DELEGATED_TOPIC_V1",
    "EnumBudgetAction",
    "EnumQualityContractMode",
    "EnumQualityGateCategory",
    "ModelBaselineIntent",
    "ModelBifrostDelegationConfig",
    "ModelBudgetLimits",
    "ModelComplianceLoopResult",
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
    "ModelDelegateSkillSavingsProjection",
    "ModelDelegateSkillTerminalProjection",
    "ModelDelegationBackendConfig",
    "ModelDelegationCircuitBreakerConfig",
    "ModelDelegationConfig",
    "ModelDelegationEventEnvelope",
    "ModelDelegationEventProjectionRow",
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
    "ModelRoutingIntent",
    "ModelRoutingTier",
    "ModelTaskDelegatedEvent",
    "ModelTierModel",
    "validate_acceptance_criteria",
]
