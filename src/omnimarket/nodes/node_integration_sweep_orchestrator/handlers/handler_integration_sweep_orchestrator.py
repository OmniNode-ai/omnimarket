from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_result import (
    ModelIntegrationSweepOrchestratorResult,
)


class HandlerIntegrationSweepOrchestrator:
    """STUB: not yet implemented. Raises SkillRoutingError."""

    def handle(
        self, request: ModelIntegrationSweepOrchestratorRequest
    ) -> ModelIntegrationSweepOrchestratorResult:
        raise NotImplementedError(  # stub-ok
            "node_integration_sweep_orchestrator is not yet implemented (OMN-8758). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
