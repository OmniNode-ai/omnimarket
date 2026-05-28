from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.evidence_pipeline_native import (
    EvidencePublisherAdapter,
    TypedEvidenceEvent,
    coerce_evidence_event,
    publish_evidence,
)


class HandlerEvidencePublisherEffect:
    """Publish typed evidence/readiness events through an adapter."""

    def __init__(self, adapter: EvidencePublisherAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self, request: TypedEvidenceEvent | Mapping[str, object]
    ) -> TypedEvidenceEvent:
        return publish_evidence(coerce_evidence_event(request), adapter=self._adapter)
