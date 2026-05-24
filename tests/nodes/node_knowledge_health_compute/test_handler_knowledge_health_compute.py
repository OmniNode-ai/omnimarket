"""Tests for deterministic knowledge health classification (compute node)."""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState
from omnimarket.nodes.node_knowledge_health_compute.handlers.handler_knowledge_health_compute import (
    HandlerKnowledgeHealthCompute,
)
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_backend_probe import (
    ModelKnowledgeBackendProbe,
)
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_health_compute_request import (
    ModelKnowledgeHealthComputeRequest,
)
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_health_report import (
    ModelKnowledgeHealthReport,
)


def _probe(
    backend_id: str,
    freshness_state: EnumKnowledgeFreshnessState = EnumKnowledgeFreshnessState.FRESH,
    entry_count: int = 100,
    last_updated_seconds_ago: int | None = 3600,
    drift_detected: bool = False,
    error: str | None = None,
) -> ModelKnowledgeBackendProbe:
    return ModelKnowledgeBackendProbe(
        backend_id=backend_id,
        freshness_state=freshness_state,
        entry_count=entry_count,
        last_updated_seconds_ago=last_updated_seconds_ago,
        drift_detected=drift_detected,
        error=error,
    )


@pytest.mark.unit
class TestHandlerKnowledgeHealthCompute:
    def test_all_fresh_backends_yields_healthy(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise"),
                _probe("qdrant"),
                _probe("memgraph"),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert isinstance(report, ModelKnowledgeHealthReport)
        assert report.overall_status == "healthy"

    def test_one_stale_backend_yields_degraded(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise", freshness_state=EnumKnowledgeFreshnessState.STALE),
                _probe("qdrant"),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert report.overall_status == "degraded"

    def test_one_unavailable_backend_yields_unhealthy(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe(
                    "repowise",
                    freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                    entry_count=0,
                    last_updated_seconds_ago=None,
                    error="connection refused",
                ),
                _probe("qdrant"),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert report.overall_status == "unhealthy"

    def test_degraded_backend_yields_unhealthy(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe(
                    "memgraph",
                    freshness_state=EnumKnowledgeFreshnessState.DEGRADED,
                ),
                _probe("qdrant"),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert report.overall_status == "unhealthy"

    def test_per_backend_status_in_report(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise", freshness_state=EnumKnowledgeFreshnessState.FRESH),
                _probe("qdrant", freshness_state=EnumKnowledgeFreshnessState.STALE),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        statuses = {s.backend_id: s for s in report.backend_statuses}
        assert statuses["repowise"].freshness_state == EnumKnowledgeFreshnessState.FRESH
        assert statuses["qdrant"].freshness_state == EnumKnowledgeFreshnessState.STALE

    def test_drift_detected_yields_degraded(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise", drift_detected=True),
                _probe("qdrant"),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert report.overall_status == "degraded"

    def test_unknown_state_yields_degraded(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise", freshness_state=EnumKnowledgeFreshnessState.UNKNOWN),
                _probe("qdrant"),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert report.overall_status == "degraded"

    def test_empty_backends_yields_degraded(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(backend_probes=())
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert report.overall_status == "degraded"

    def test_deterministic_repeated_invocations(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise", freshness_state=EnumKnowledgeFreshnessState.STALE),
                _probe("qdrant"),
            )
        )
        handler = HandlerKnowledgeHealthCompute()

        r1 = handler.handle(request)
        r2 = handler.handle(request)
        assert r1.overall_status == r2.overall_status
        assert r1.backend_statuses == r2.backend_statuses

    def test_recommendations_populated_for_stale_backend(self) -> None:
        request = ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                _probe("repowise", freshness_state=EnumKnowledgeFreshnessState.STALE),
            )
        )
        report = HandlerKnowledgeHealthCompute().handle(request)

        assert len(report.recommendations) > 0
