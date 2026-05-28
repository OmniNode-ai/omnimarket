from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_bundle import (
    ModelEvidenceBundle,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.nodes.evidence_pipeline_native import coerce_bundle, match_contract


class HandlerContractMatcherCompute:
    """Pure non-LLM contract/evidence matching."""

    def handle(
        self, request: ModelEvidenceBundle | Mapping[str, object]
    ) -> ModelEvidenceValidationResult:
        return match_contract(coerce_bundle(request))
