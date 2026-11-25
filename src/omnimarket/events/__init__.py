# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event models for cross-node event contracts within omnimarket."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

# OMN-26336: the public names resolve on first access, so importing one submodule of this
# package (or the package root) no longer imports every event module on the importing thread.
_EXPORTS: dict[str, str] = {
    "DEFAULT_PREVIOUS_IMAGE": "omnimarket.events.runtime_deployment",
    "EmitClient": "omnimarket.events.emit_client",
    "EnumArmActionMode": "omnimarket.events.pr_arm_gate",
    "EnumArmDecision": "omnimarket.events.pr_arm_gate",
    "EnumBuildSource": "omnimarket.events.runtime_deployment",
    "EnumBundleStatus": "omnimarket.events.knowledge_context",
    "EnumDelegationJudgeVerdict": "omnimarket.events.delegation_judge_verdict",
    "EnumFailureOrigin": "omnimarket.events.repo_health",
    "EnumFragmentSource": "omnimarket.events.knowledge_context",
    "EnumGoldenChainGenerationStatus": "omnimarket.events.generation",
    "EnumOccGateState": "omnimarket.events.runtime_deployment",
    "EnumPromotionClass": "omnimarket.events.runtime_deployment",
    "EnumRedeployPhase": "omnimarket.events.runtime_deployment",
    "EnumRedeployScope": "omnimarket.events.runtime_deployment",
    "EnumRedeployStatus": "omnimarket.events.runtime_deployment",
    "EnumTestGenerationStatus": "omnimarket.events.generation",
    "ModelArmCandidate": "omnimarket.events.pr_arm_gate",
    "ModelArmGateDecision": "omnimarket.events.pr_arm_gate",
    "ModelArmGatePolicy": "omnimarket.events.pr_arm_gate",
    "ModelArmGateRequest": "omnimarket.events.pr_arm_gate",
    "ModelCheckpointRequest": "omnimarket.events.checkpoint",
    "ModelDaemonHealthProbeResult": "omnimarket.events.daemon_health_probe",
    "ModelDashboardProjectionEvent": "omnimarket.events.evidence_dashboard",
    "ModelDeferredChainWarning": "omnimarket.events.generation",
    "ModelDelegationAcceptanceJudgedEvent": "omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event",
    "ModelDelegationJudgeVerdictEvent": "omnimarket.events.delegation_judge_verdict",
    "ModelDeployRebuildCommand": "omnimarket.events.runtime_deployment",
    "ModelDeployRebuildCompleted": "omnimarket.events.runtime_deployment",
    "ModelGeneratedTestFile": "omnimarket.events.generation",
    "ModelGoldenChainGenerationRequest": "omnimarket.events.generation",
    "ModelGoldenChainGenerationResult": "omnimarket.events.generation",
    "ModelIntentClassifiedEnvelope": "omnimarket.intelligence.events",
    "ModelIntentDriftDetectedEnvelope": "omnimarket.intelligence.events",
    "ModelIntentOutcomeLabeledEnvelope": "omnimarket.intelligence.events",
    "ModelIntentPatternPromotedEnvelope": "omnimarket.intelligence.events",
    "ModelKnowledgeContextBundle": "omnimarket.events.knowledge_context",
    "ModelKnowledgeContextFragment": "omnimarket.events.knowledge_context",
    "ModelKnowledgeContextState": "omnimarket.events.knowledge_context",
    "ModelLedgerAppendedEvent": "omnimarket.events.ledger",
    "ModelLedgerHashComputed": "omnimarket.events.ledger",
    "ModelLlmDelegationCallRequest": "omnimarket.events.llm_delegation_call",
    "ModelPlanToTicketsCompletedEvent": "omnimarket.events.design_to_plan",
    "ModelPlanToTicketsStartCommand": "omnimarket.events.design_to_plan",
    "ModelProdPromotionGateDecision": "omnimarket.events.runtime_deployment",
    "ModelProdPromotionInputs": "omnimarket.events.runtime_deployment",
    "ModelReadinessProjectionFact": "omnimarket.events.runtime_deployment",
    "ModelRedeployCommand": "omnimarket.events.runtime_deployment",
    "ModelRedeployCompletedEvent": "omnimarket.events.runtime_deployment",
    "ModelRedeployResult": "omnimarket.events.runtime_deployment",
    "ModelRedeployRolledBackEvent": "omnimarket.events.runtime_deployment",
    "ModelRedeployState": "omnimarket.events.runtime_deployment",
    "ModelRepoHealthClassification": "omnimarket.events.repo_health",
    "ModelRepoHealthFailureEnvelope": "omnimarket.events.repo_health",
    "ModelRuntimeDeploymentProof": "omnimarket.events.runtime_deployment",
    "ModelRuntimeImageBuilt": "omnimarket.events.runtime_deployment",
    "ModelSwarmFanoutResult": "omnimarket.events.swarm_fanout",
    "ModelTestGenerationRequest": "omnimarket.events.generation",
    "ModelTestGenerationResult": "omnimarket.events.generation",
    "ModelTopicActivitySample": "omnimarket.events.topic_activity",
    "ModelTopicActivitySampleEvent": "omnimarket.events.topic_activity",
    "RuntimeLaneLike": "omnimarket.events.runtime_deployment",
    "build_delegation_judge_verdict_event": "omnimarket.events.delegation_judge_verdict",
    "default_socket_path": "omnimarket.events.emit_client",
    "evaluate_prod_digest_gate": "omnimarket.events.runtime_deployment",
    "evaluate_prod_promotion_gate": "omnimarket.events.runtime_deployment",
    "judged_acceptance_event_id_for": "omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event",
    "lane_target": "omnimarket.events.runtime_deployment",
    "sha256_json": "omnimarket.events.delegation_judge_verdict",
    "sha256_text": "omnimarket.events.delegation_judge_verdict",
}

if TYPE_CHECKING:
    from omnimarket.events.checkpoint import (
        ModelCheckpointRequest as ModelCheckpointRequest,
    )
    from omnimarket.events.daemon_health_probe import (
        ModelDaemonHealthProbeResult as ModelDaemonHealthProbeResult,
    )
    from omnimarket.events.delegation_judge_verdict import (
        EnumDelegationJudgeVerdict as EnumDelegationJudgeVerdict,
    )
    from omnimarket.events.delegation_judge_verdict import (
        ModelDelegationJudgeVerdictEvent as ModelDelegationJudgeVerdictEvent,
    )
    from omnimarket.events.delegation_judge_verdict import (
        build_delegation_judge_verdict_event as build_delegation_judge_verdict_event,
    )
    from omnimarket.events.delegation_judge_verdict import (
        sha256_json as sha256_json,
    )
    from omnimarket.events.delegation_judge_verdict import (
        sha256_text as sha256_text,
    )
    from omnimarket.events.design_to_plan import (
        ModelPlanToTicketsCompletedEvent as ModelPlanToTicketsCompletedEvent,
    )
    from omnimarket.events.design_to_plan import (
        ModelPlanToTicketsStartCommand as ModelPlanToTicketsStartCommand,
    )
    from omnimarket.events.emit_client import (
        EmitClient as EmitClient,
    )
    from omnimarket.events.emit_client import (
        default_socket_path as default_socket_path,
    )
    from omnimarket.events.evidence_dashboard import (
        ModelDashboardProjectionEvent as ModelDashboardProjectionEvent,
    )
    from omnimarket.events.generation import (
        EnumGoldenChainGenerationStatus as EnumGoldenChainGenerationStatus,
    )
    from omnimarket.events.generation import (
        EnumTestGenerationStatus as EnumTestGenerationStatus,
    )
    from omnimarket.events.generation import (
        ModelDeferredChainWarning as ModelDeferredChainWarning,
    )
    from omnimarket.events.generation import (
        ModelGeneratedTestFile as ModelGeneratedTestFile,
    )
    from omnimarket.events.generation import (
        ModelGoldenChainGenerationRequest as ModelGoldenChainGenerationRequest,
    )
    from omnimarket.events.generation import (
        ModelGoldenChainGenerationResult as ModelGoldenChainGenerationResult,
    )
    from omnimarket.events.generation import (
        ModelTestGenerationRequest as ModelTestGenerationRequest,
    )
    from omnimarket.events.generation import (
        ModelTestGenerationResult as ModelTestGenerationResult,
    )
    from omnimarket.events.knowledge_context import (
        EnumBundleStatus as EnumBundleStatus,
    )
    from omnimarket.events.knowledge_context import (
        EnumFragmentSource as EnumFragmentSource,
    )
    from omnimarket.events.knowledge_context import (
        ModelKnowledgeContextBundle as ModelKnowledgeContextBundle,
    )
    from omnimarket.events.knowledge_context import (
        ModelKnowledgeContextFragment as ModelKnowledgeContextFragment,
    )
    from omnimarket.events.knowledge_context import (
        ModelKnowledgeContextState as ModelKnowledgeContextState,
    )
    from omnimarket.events.ledger import (
        ModelLedgerAppendedEvent as ModelLedgerAppendedEvent,
    )
    from omnimarket.events.ledger import (
        ModelLedgerHashComputed as ModelLedgerHashComputed,
    )
    from omnimarket.events.llm_delegation_call import (
        ModelLlmDelegationCallRequest as ModelLlmDelegationCallRequest,
    )
    from omnimarket.events.pr_arm_gate import (
        EnumArmActionMode as EnumArmActionMode,
    )
    from omnimarket.events.pr_arm_gate import (
        EnumArmDecision as EnumArmDecision,
    )
    from omnimarket.events.pr_arm_gate import (
        ModelArmCandidate as ModelArmCandidate,
    )
    from omnimarket.events.pr_arm_gate import (
        ModelArmGateDecision as ModelArmGateDecision,
    )
    from omnimarket.events.pr_arm_gate import (
        ModelArmGatePolicy as ModelArmGatePolicy,
    )
    from omnimarket.events.pr_arm_gate import (
        ModelArmGateRequest as ModelArmGateRequest,
    )
    from omnimarket.events.repo_health import (
        EnumFailureOrigin as EnumFailureOrigin,
    )
    from omnimarket.events.repo_health import (
        ModelRepoHealthClassification as ModelRepoHealthClassification,
    )
    from omnimarket.events.repo_health import (
        ModelRepoHealthFailureEnvelope as ModelRepoHealthFailureEnvelope,
    )
    from omnimarket.events.runtime_deployment import (
        DEFAULT_PREVIOUS_IMAGE as DEFAULT_PREVIOUS_IMAGE,
    )
    from omnimarket.events.runtime_deployment import (
        EnumBuildSource as EnumBuildSource,
    )
    from omnimarket.events.runtime_deployment import (
        EnumOccGateState as EnumOccGateState,
    )
    from omnimarket.events.runtime_deployment import (
        EnumPromotionClass as EnumPromotionClass,
    )
    from omnimarket.events.runtime_deployment import (
        EnumRedeployPhase as EnumRedeployPhase,
    )
    from omnimarket.events.runtime_deployment import (
        EnumRedeployScope as EnumRedeployScope,
    )
    from omnimarket.events.runtime_deployment import (
        EnumRedeployStatus as EnumRedeployStatus,
    )
    from omnimarket.events.runtime_deployment import (
        ModelDeployRebuildCommand as ModelDeployRebuildCommand,
    )
    from omnimarket.events.runtime_deployment import (
        ModelDeployRebuildCompleted as ModelDeployRebuildCompleted,
    )
    from omnimarket.events.runtime_deployment import (
        ModelProdPromotionGateDecision as ModelProdPromotionGateDecision,
    )
    from omnimarket.events.runtime_deployment import (
        ModelProdPromotionInputs as ModelProdPromotionInputs,
    )
    from omnimarket.events.runtime_deployment import (
        ModelReadinessProjectionFact as ModelReadinessProjectionFact,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRedeployCommand as ModelRedeployCommand,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRedeployCompletedEvent as ModelRedeployCompletedEvent,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRedeployResult as ModelRedeployResult,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRedeployRolledBackEvent as ModelRedeployRolledBackEvent,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRedeployState as ModelRedeployState,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRuntimeDeploymentProof as ModelRuntimeDeploymentProof,
    )
    from omnimarket.events.runtime_deployment import (
        ModelRuntimeImageBuilt as ModelRuntimeImageBuilt,
    )
    from omnimarket.events.runtime_deployment import (
        RuntimeLaneLike as RuntimeLaneLike,
    )
    from omnimarket.events.runtime_deployment import (
        evaluate_prod_digest_gate as evaluate_prod_digest_gate,
    )
    from omnimarket.events.runtime_deployment import (
        evaluate_prod_promotion_gate as evaluate_prod_promotion_gate,
    )
    from omnimarket.events.runtime_deployment import (
        lane_target as lane_target,
    )
    from omnimarket.events.swarm_fanout import (
        ModelSwarmFanoutResult as ModelSwarmFanoutResult,
    )
    from omnimarket.events.topic_activity import (
        ModelTopicActivitySample as ModelTopicActivitySample,
    )
    from omnimarket.events.topic_activity import (
        ModelTopicActivitySampleEvent as ModelTopicActivitySampleEvent,
    )
    from omnimarket.intelligence.events import (
        ModelIntentClassifiedEnvelope as ModelIntentClassifiedEnvelope,
    )
    from omnimarket.intelligence.events import (
        ModelIntentDriftDetectedEnvelope as ModelIntentDriftDetectedEnvelope,
    )
    from omnimarket.intelligence.events import (
        ModelIntentOutcomeLabeledEnvelope as ModelIntentOutcomeLabeledEnvelope,
    )
    from omnimarket.intelligence.events import (
        ModelIntentPatternPromotedEnvelope as ModelIntentPatternPromotedEnvelope,
    )
    from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
        ModelDelegationAcceptanceJudgedEvent as ModelDelegationAcceptanceJudgedEvent,
    )
    from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
        judged_acceptance_event_id_for as judged_acceptance_event_id_for,
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
    "ModelDelegationAcceptanceJudgedEvent",
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
    "ModelTopicActivitySample",
    "ModelTopicActivitySampleEvent",
    "RuntimeLaneLike",
    "build_delegation_judge_verdict_event",
    "default_socket_path",
    "evaluate_prod_digest_gate",
    "evaluate_prod_promotion_gate",
    "judged_acceptance_event_id_for",
    "lane_target",
    "sha256_json",
    "sha256_text",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})
