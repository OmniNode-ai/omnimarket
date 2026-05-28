from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.nodes.evidence_pipeline_native import (
    LinearEvidenceUpdaterAdapter,
    coerce_validation,
    update_linear,
)


class HandlerLinearUpdaterEffect:
    """Write advisory Linear evidence annotations through an adapter."""

    def __init__(self, adapter: LinearEvidenceUpdaterAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self, request: ModelEvidenceValidationResult | Mapping[str, object]
    ) -> ModelEvidenceValidationResult:
        return update_linear(coerce_validation(request), adapter=self._adapter)
