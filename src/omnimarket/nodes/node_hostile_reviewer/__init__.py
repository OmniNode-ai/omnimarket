"""node_hostile_reviewer — Multi-model adversarial code review WorkflowPackage."""

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_convergence_reducer import (
    ModelConvergenceInput,
    ModelConvergenceOutput,
    ModelFindingLabel,
    compute_convergence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_hostile_reviewer import (
    HandlerHostileReviewer,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser import (
    EnumParseStatus,
    ModelParseResult,
    parse_model_response,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_review_orchestrator import (
    ModelInferenceAdapter,
    ModelMergedFinding,
    ModelOrchestratorInput,
    ModelOrchestratorOutput,
    ModelPerModelResult,
    run_review_orchestration,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_workflow_runner import (
    ModelWorkflowInput,
    ModelWorkflowOutput,
    run_hostile_review_workflow,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_completed_event import (
    ModelHostileReviewerCompletedEvent,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_phase_event import (
    ModelHostileReviewerPhaseEvent,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
    ModelHostileReviewerState,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    EnumReviewVerdict,
    ModelFindingEvidence,
    ModelReviewFinding,
)

__all__ = [
    "AdapterInferenceBridge",
    "EnumFindingCategory",
    "EnumFindingSeverity",
    "EnumHostileReviewerPhase",
    "EnumParseStatus",
    "EnumReviewConfidence",
    "EnumReviewVerdict",
    "HandlerHostileReviewer",
    "ModelConvergenceInput",
    "ModelConvergenceOutput",
    "ModelFindingEvidence",
    "ModelFindingLabel",
    "ModelHostileReviewerCompletedEvent",
    "ModelHostileReviewerPhaseEvent",
    "ModelHostileReviewerStartCommand",
    "ModelHostileReviewerState",
    "ModelInferenceAdapter",
    "ModelInferenceBridgeConfig",
    "ModelMergedFinding",
    "ModelOrchestratorInput",
    "ModelOrchestratorOutput",
    "ModelParseResult",
    "ModelPerModelResult",
    "ModelPromptBuilderInput",
    "ModelPromptBuilderOutput",
    "ModelReviewFinding",
    "ModelWorkflowInput",
    "ModelWorkflowOutput",
    "NodeHostileReviewer",
    "build_prompt",
    "compute_convergence",
    "parse_model_response",
    "run_hostile_review_workflow",
    "run_review_orchestration",
]
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    HandlerPromptBuilder,
)


class NodeHostileReviewer(HandlerPromptBuilder):
    """ONEX entry-point wrapper for HandlerPromptBuilder."""
