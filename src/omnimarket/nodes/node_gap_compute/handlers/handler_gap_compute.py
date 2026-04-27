from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    ModelGapComputeRequest,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_result import (
    ModelGapComputeResult,
)


class HandlerGapCompute:
    """STUB: not yet implemented. Raises SkillRoutingError."""

    def handle(self, request: ModelGapComputeRequest) -> ModelGapComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_gap_compute is not yet implemented (OMN-8761). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
