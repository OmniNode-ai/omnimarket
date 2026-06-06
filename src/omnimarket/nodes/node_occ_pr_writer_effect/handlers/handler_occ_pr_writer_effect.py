from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_occ_pr_reference import (
    ModelOccPrReference,
)

from omnimarket.nodes.evidence_pipeline_native import (
    OccPrWriterAdapter,
    coerce_validation,
    write_occ_pr,
)


class HandlerOccPrWriterEffect:
    """Create or reuse OCC PR references through an injected adapter."""

    def __init__(self, adapter: OccPrWriterAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self, request: ModelEvidenceValidationResult | Mapping[str, object]
    ) -> ModelOccPrReference:
        return write_occ_pr(coerce_validation(request), adapter=self._adapter)
