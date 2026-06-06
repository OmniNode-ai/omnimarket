from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_gap_report import (
    ModelGapReport,
)

from omnimarket.nodes.evidence_pipeline_native import analyze_gaps, coerce_validation


class HandlerGapAnalyzerCompute:
    """Reduce validation results into typed deployment gap classifications."""

    def handle(
        self, request: ModelEvidenceValidationResult | Mapping[str, object]
    ) -> ModelGapReport:
        return analyze_gaps(coerce_validation(request))
