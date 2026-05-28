from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_bundle import (
    ModelEvidenceBundle,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_raw_evidence_payload import (
    ModelRawEvidencePayload,
)

from omnimarket.nodes.evidence_pipeline_native import coerce_raw, extract_evidence


class HandlerEvidenceExtractorCompute:
    """Pure deterministic extraction from raw evidence to typed bundle."""

    def handle(
        self, request: ModelRawEvidencePayload | Mapping[str, object]
    ) -> ModelEvidenceBundle:
        return extract_evidence(coerce_raw(request))
