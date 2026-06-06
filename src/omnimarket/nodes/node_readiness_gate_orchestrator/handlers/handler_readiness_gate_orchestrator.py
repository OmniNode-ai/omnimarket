from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_gap_report import (
    ModelGapReport,
)

from omnimarket.nodes.evidence_pipeline_native import (
    NativeReadinessGatePorts,
    ReadinessGatePorts,
    coerce_gap,
    coerce_readiness,
)


class HandlerReadinessGateOrchestrator:
    """Emit the authoritative readiness gate result through typed ports."""

    def __init__(self, ports: ReadinessGatePorts | None = None) -> None:
        self._ports = ports or NativeReadinessGatePorts()

    def handle(
        self,
        request: ModelGapReport | ModelDeploymentReadinessResult | Mapping[str, object],
    ) -> ModelDeploymentReadinessResult:
        if isinstance(request, ModelDeploymentReadinessResult) or (
            isinstance(request, Mapping) and "readiness_state" in request
        ):
            readiness = coerce_readiness(request)
        else:
            readiness = self._ports.score(coerce_gap(request))
        return self._ports.publish_gate(readiness)
