from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_gap_report import (
    ModelGapReport,
)

from omnimarket.nodes.evidence_pipeline_native import coerce_gap, score_readiness


class HandlerReadinessScorerCompute:
    """Pure deterministic scorer for deployment readiness."""

    def handle(
        self, request: ModelGapReport | Mapping[str, object]
    ) -> ModelDeploymentReadinessResult:
        return score_readiness(coerce_gap(request))
