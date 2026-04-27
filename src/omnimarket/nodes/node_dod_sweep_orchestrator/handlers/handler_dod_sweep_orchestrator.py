from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_result import (
    ModelDodSweepOrchestratorResult,
)


class HandlerDodSweepOrchestrator:
    """STUB: not yet implemented. Raises SkillRoutingError."""

    def handle(
        self, request: ModelDodSweepOrchestratorRequest
    ) -> ModelDodSweepOrchestratorResult:
        raise NotImplementedError(  # stub-ok
            "node_dod_sweep_orchestrator is not yet implemented (OMN-8759). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
