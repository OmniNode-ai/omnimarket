# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event models for cross-node event contracts within omnimarket."""

from omnimarket.events.checkpoint import ModelCheckpointRequest
from omnimarket.events.daemon_health_probe import ModelDaemonHealthProbeResult
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
from omnimarket.events.runtime_deployment import (
    DEFAULT_PREVIOUS_IMAGE,
    EnumBuildSource,
    EnumOccGateState,
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
    RuntimeLaneLike,
    evaluate_prod_digest_gate,
    evaluate_prod_promotion_gate,
    lane_target,
)
from omnimarket.intelligence.events import (
    ModelIntentClassifiedEnvelope,
    ModelIntentDriftDetectedEnvelope,
    ModelIntentOutcomeLabeledEnvelope,
    ModelIntentPatternPromotedEnvelope,
)

__all__ = [
    "DEFAULT_PREVIOUS_IMAGE",
    "EmitClient",
    "EnumBuildSource",
    "EnumBundleStatus",
    "EnumFragmentSource",
    "EnumGoldenChainGenerationStatus",
    "EnumOccGateState",
    "EnumRedeployPhase",
    "EnumRedeployScope",
    "EnumRedeployStatus",
    "EnumTestGenerationStatus",
    "ModelCheckpointRequest",
    "ModelDaemonHealthProbeResult",
    "ModelDashboardProjectionEvent",
    "ModelDeferredChainWarning",
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
    "ModelRuntimeDeploymentProof",
    "ModelTestGenerationRequest",
    "ModelTestGenerationResult",
    "RuntimeLaneLike",
    "default_socket_path",
    "evaluate_prod_digest_gate",
    "evaluate_prod_promotion_gate",
    "lane_target",
]
