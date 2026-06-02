# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event models for cross-node event contracts within omnimarket."""

from omnimarket.events.checkpoint import ModelCheckpointRequest
from omnimarket.events.daemon_health_probe import ModelDaemonHealthProbeResult
from omnimarket.events.design_to_plan import (
    ModelPlanToTicketsCompletedEvent,
    ModelPlanToTicketsStartCommand,
)
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
    ModelRuntimeDeploymentProof,
    RuntimeLaneLike,
)
from omnimarket.intelligence.events import (
    ModelIntentClassifiedEnvelope,
    ModelIntentDriftDetectedEnvelope,
    ModelIntentOutcomeLabeledEnvelope,
    ModelIntentPatternPromotedEnvelope,
)

__all__ = [
    "EnumBundleStatus",
    "EnumFragmentSource",
    "EnumGoldenChainGenerationStatus",
    "EnumTestGenerationStatus",
    "ModelCheckpointRequest",
    "ModelDaemonHealthProbeResult",
    "ModelDashboardProjectionEvent",
    "ModelDeferredChainWarning",
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
    "ModelRuntimeDeploymentProof",
    "ModelTestGenerationRequest",
    "ModelTestGenerationResult",
    "RuntimeLaneLike",
]
