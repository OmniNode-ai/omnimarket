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
    _unwrap_envelope,
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
        # Unwrap any transport envelope before deciding the dispatch branch so a
        # ``readiness_state`` nested under ``.payload`` is not misrouted to the
        # gap-scoring path.
        unwrapped = _unwrap_envelope(request)
        if isinstance(unwrapped, ModelDeploymentReadinessResult) or (
            isinstance(unwrapped, Mapping) and "readiness_state" in unwrapped
        ):
            readiness = coerce_readiness(unwrapped)
        else:
            readiness = self._ports.score(coerce_gap(unwrapped))
        return self._ports.publish_gate(readiness)
