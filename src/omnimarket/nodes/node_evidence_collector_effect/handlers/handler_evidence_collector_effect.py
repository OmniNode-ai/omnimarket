from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_raw_evidence_payload import (
    ModelRawEvidencePayload,
)

from omnimarket.nodes.evidence_pipeline_native import (
    EvidenceCollectorAdapter,
    coerce_command,
    collect_evidence,
)


class HandlerEvidenceCollectorEffect:
    """Gather raw evidence through an explicit collector adapter boundary."""

    def __init__(self, adapter: EvidenceCollectorAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self, request: ModelEvidencePipelineCommand | Mapping[str, object]
    ) -> ModelRawEvidencePayload:
        command = coerce_command(request)
        return collect_evidence(command, adapter=self._adapter)
