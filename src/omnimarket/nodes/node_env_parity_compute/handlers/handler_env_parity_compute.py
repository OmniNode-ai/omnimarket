from omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_request import (
    ModelEnvParityComputeRequest,
)
from omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_result import (
    ModelEnvParityComputeResult,
)


class HandlerEnvParityCompute:
    """STUB: not yet implemented. Raises SkillRoutingError."""

    def handle(
        self, request: ModelEnvParityComputeRequest
    ) -> ModelEnvParityComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_env_parity_compute is not yet implemented (OMN-8763). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
