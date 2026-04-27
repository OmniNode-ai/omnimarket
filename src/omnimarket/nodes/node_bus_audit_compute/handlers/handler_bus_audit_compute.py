from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_request import (
    ModelBusAuditComputeRequest,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_result import (
    ModelBusAuditComputeResult,
)


class HandlerBusAuditCompute:
    """STUB: not yet implemented. Raises SkillRoutingError."""

    def handle(
        self, request: ModelBusAuditComputeRequest
    ) -> ModelBusAuditComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_bus_audit_compute is not yet implemented (OMN-8760). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
