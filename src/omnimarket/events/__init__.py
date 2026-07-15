# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event models for cross-node event contracts within omnimarket."""

from omnimarket.events.checkpoint import ModelCheckpointRequest
from omnimarket.events.daemon_health_probe import ModelDaemonHealthProbeResult
from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
    build_delegation_judge_verdict_event,
    sha256_json,
    sha256_text,
)
from omnimarket.events.design_to_plan import (
    ModelPlanToTicketsCompletedEvent,
    ModelPlanToTicketsStartCommand,
)
from omnimarket.events.emit_client import EmitClient, default_socket_path
from omnimarket.events.evidence_dashboard import ModelDashboardProjectionEvent
from omnimarket.events.generation import (
    EnumGoldenChainGenerationStatus,
    EnumTestGenerationStatus,
    ModelDeferredChainWarning,
    ModelGeneratedTestFile,
    ModelGoldenChainGenerationRequest,
    ModelGoldenChainGenerationResult,
    ModelTestGenerationRequest,
    ModelTestGenerationResult,
)
from omnimarket.events.knowledge_context import (
    EnumBundleStatus,
    EnumFragmentSource,
    ModelKnowledgeContextBundle,
    ModelKnowledgeContextFragment,
    ModelKnowledgeContextState,
)
from omnimarket.events.ledger import ModelLedgerAppendedEvent, ModelLedgerHashComputed
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.events.pr_arm_gate import (
    EnumArmActionMode,
    EnumArmDecision,
    ModelArmCandidate,
    ModelArmGateDecision,
    ModelArmGatePolicy,
    ModelArmGateRequest,
)
from omnimarket.events.repo_health import (
    EnumFailureOrigin,
    ModelRepoHealthClassification,
    ModelRepoHealthFailureEnvelope,
)
from omnimarket.events.runtime_deployment import (
    DEFAULT_PREVIOUS_IMAGE,
    EnumBuildSource,
    EnumOccGateState,
    EnumPromotionClass,
    EnumRedeployPhase,
    EnumRedeployScope,
    EnumRedeployStatus,
    ModelDeployRebuildCommand,
    ModelDeployRebuildCompleted,
    ModelProdPromotionGateDecision,
    ModelProdPromotionInputs,
    ModelReadinessProjectionFact,
    ModelRedeployCommand,
    ModelRedeployCompletedEvent,
    ModelRedeployResult,
    ModelRedeployRolledBackEvent,
    ModelRedeployState,
    ModelRuntimeDeploymentProof,
    ModelRuntimeImageBuilt,
    RuntimeLaneLike,
    evaluate_prod_digest_gate,
    evaluate_prod_promotion_gate,
    lane_target,
)
from omnimarket.events.swarm_fanout import ModelSwarmFanoutResult
from omnimarket.intelligence.events import (
    ModelIntentClassifiedEnvelope,
    ModelIntentDriftDetectedEnvelope,
    ModelIntentOutcomeLabeledEnvelope,
    ModelIntentPatternPromotedEnvelope,
)

__all__ = [
    "DEFAULT_PREVIOUS_IMAGE",
    "EmitClient",
    "EnumArmActionMode",
    "EnumArmDecision",
    "EnumBuildSource",
    "EnumBundleStatus",
    "EnumDelegationJudgeVerdict",
    "EnumFailureOrigin",
    "EnumFragmentSource",
    "EnumGoldenChainGenerationStatus",
    "EnumOccGateState",
    "EnumPromotionClass",
    "EnumRedeployPhase",
    "EnumRedeployScope",
    "EnumRedeployStatus",
    "EnumTestGenerationStatus",
    "ModelArmCandidate",
    "ModelArmGateDecision",
    "ModelArmGatePolicy",
    "ModelArmGateRequest",
    "ModelCheckpointRequest",
    "ModelDaemonHealthProbeResult",
    "ModelDashboardProjectionEvent",
    "ModelDeferredChainWarning",
    "ModelDelegationJudgeVerdictEvent",
    "ModelDeployRebuildCommand",
    "ModelDeployRebuildCompleted",
    "ModelGeneratedTestFile",
    "ModelGoldenChainGenerationRequest",
    "ModelGoldenChainGenerationResult",
    "ModelIntentClassifiedEnvelope",
    "ModelIntentDriftDetectedEnvelope",
    "ModelIntentOutcomeLabeledEnvelope",
    "ModelIntentPatternPromotedEnvelope",
    "ModelKnowledgeContextBundle",
    "ModelKnowledgeContextFragment",
    "ModelKnowledgeContextState",
    "ModelLedgerAppendedEvent",
    "ModelLedgerHashComputed",
    "ModelLlmDelegationCallRequest",
    "ModelPlanToTicketsCompletedEvent",
    "ModelPlanToTicketsStartCommand",
    "ModelProdPromotionGateDecision",
    "ModelProdPromotionInputs",
    "ModelReadinessProjectionFact",
    "ModelRedeployCommand",
    "ModelRedeployCompletedEvent",
    "ModelRedeployResult",
    "ModelRedeployRolledBackEvent",
    "ModelRedeployState",
    "ModelRepoHealthClassification",
    "ModelRepoHealthFailureEnvelope",
    "ModelRuntimeDeploymentProof",
    "ModelRuntimeImageBuilt",
    "ModelSwarmFanoutResult",
    "ModelTestGenerationRequest",
    "ModelTestGenerationResult",
    "RuntimeLaneLike",
    "build_delegation_judge_verdict_event",
    "default_socket_path",
    "evaluate_prod_digest_gate",
    "evaluate_prod_promotion_gate",
    "lane_target",
    "sha256_json",
    "sha256_text",
]
