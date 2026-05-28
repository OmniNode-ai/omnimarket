from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_occ_pr_reference import (
    ModelOccPrReference,
)

from omnimarket.nodes.evidence_pipeline_native import (
    EvidencePipelinePorts,
    NativeEvidencePipelinePorts,
    coerce_command,
)


class HandlerEvidencePipelineOrchestrator:
    """Coordinate the native evidence path through typed node ports."""

    def __init__(self, ports: EvidencePipelinePorts | None = None) -> None:
        self._ports = ports or NativeEvidencePipelinePorts()

    def handle(
        self, request: ModelEvidencePipelineCommand | Mapping[str, object]
    ) -> ModelOccPrReference:
        command = coerce_command(request)
        raw = self._ports.collect(command)
        bundle = self._ports.extract(raw)
        validation = self._ports.match_contract(bundle)
        occ_pr = self._ports.write_occ_pr(validation)
        self._ports.update_linear(validation)
        self._ports.publish(validation)
        self._ports.publish(occ_pr)
        return occ_pr
