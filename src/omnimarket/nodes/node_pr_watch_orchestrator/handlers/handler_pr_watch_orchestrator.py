from omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_watch_orchestrator_request import (
    ModelPrWatchOrchestratorRequest,
)
from omnimarket.nodes.node_pr_watch_orchestrator.models.model_pr_watch_orchestrator_result import (
    ModelPrWatchOrchestratorResult,
)


class HandlerPrWatchOrchestrator:
    """STUB: not yet implemented. Raises SkillRoutingError."""

    def handle(
        self, request: ModelPrWatchOrchestratorRequest
    ) -> ModelPrWatchOrchestratorResult:
        raise NotImplementedError(  # stub-ok
            "node_pr_watch_orchestrator is not yet implemented (OMN-8762). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
