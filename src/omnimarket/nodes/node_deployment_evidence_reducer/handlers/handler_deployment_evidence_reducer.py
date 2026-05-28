from __future__ import annotations

from collections.abc import Mapping

from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)

from omnimarket.nodes.evidence_pipeline_native import (
    DeploymentEvidenceProjectionStore,
    TypedEvidenceEvent,
    coerce_evidence_event,
    reduce_deployment_evidence,
)


class HandlerDeploymentEvidenceReducer:
    """Materialize deployment evidence projection state from typed events."""

    def __init__(self, store: DeploymentEvidenceProjectionStore | None = None) -> None:
        self._store = store

    def handle(
        self, request: TypedEvidenceEvent | Mapping[str, object]
    ) -> ModelDeploymentReadinessResult:
        return reduce_deployment_evidence(
            coerce_evidence_event(request),
            store=self._store,
        )
