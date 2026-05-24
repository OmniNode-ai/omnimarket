# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure deterministic knowledge health classification handler."""

from __future__ import annotations

from typing import Literal

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState
from omnimarket.events.knowledge_health import ModelKnowledgeBackendProbe
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_backend_status import (
    ModelKnowledgeBackendStatus,
)
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_health_compute_request import (
    ModelKnowledgeHealthComputeRequest,
)
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_health_report import (
    ModelKnowledgeHealthReport,
)

_UNHEALTHY_STATES = frozenset(
    {EnumKnowledgeFreshnessState.DEGRADED, EnumKnowledgeFreshnessState.UNAVAILABLE}
)
_DEGRADED_STATES = frozenset(
    {EnumKnowledgeFreshnessState.STALE, EnumKnowledgeFreshnessState.UNKNOWN}
)


def _classify_overall(
    statuses: tuple[ModelKnowledgeBackendStatus, ...],
) -> Literal["healthy", "degraded", "unhealthy"]:
    if not statuses:
        return "degraded"
    if any(s.freshness_state in _UNHEALTHY_STATES for s in statuses):
        return "unhealthy"
    if any(s.freshness_state in _DEGRADED_STATES or s.drift_detected for s in statuses):
        return "degraded"
    return "healthy"


def _build_status(probe: ModelKnowledgeBackendProbe) -> ModelKnowledgeBackendStatus:
    return ModelKnowledgeBackendStatus(
        backend_id=probe.backend_id,
        freshness_state=probe.freshness_state,
        entry_count=probe.entry_count,
        last_updated_seconds_ago=probe.last_updated_seconds_ago,
        drift_detected=probe.drift_detected,
        error=probe.error,
    )


def _build_recommendations(
    statuses: tuple[ModelKnowledgeBackendStatus, ...],
) -> tuple[str, ...]:
    recs: list[str] = []
    for s in statuses:
        if s.freshness_state == EnumKnowledgeFreshnessState.STALE:
            recs.append(f"{s.backend_id}: index is stale — trigger a re-index or sync")
        elif s.freshness_state == EnumKnowledgeFreshnessState.UNAVAILABLE:
            recs.append(
                f"{s.backend_id}: backend unavailable — verify service health and connectivity"
            )
        elif s.freshness_state == EnumKnowledgeFreshnessState.DEGRADED:
            recs.append(
                f"{s.backend_id}: backend degraded — inspect error logs and restart if necessary"
            )
        elif s.freshness_state == EnumKnowledgeFreshnessState.UNKNOWN:
            recs.append(
                f"{s.backend_id}: freshness unknown — probe returned no usable data"
            )
        if s.drift_detected:
            recs.append(
                f"{s.backend_id}: drift detected — knowledge base may be out of sync with source"
            )
    return tuple(recs)


def classify_knowledge_health(
    request: ModelKnowledgeHealthComputeRequest,
) -> ModelKnowledgeHealthReport:
    """Classify backend probes into a health report. Pure — no I/O."""
    statuses = tuple(_build_status(p) for p in request.backend_probes)
    overall = _classify_overall(statuses)
    recommendations = _build_recommendations(statuses)
    return ModelKnowledgeHealthReport(
        overall_status=overall,
        backend_statuses=statuses,
        recommendations=recommendations,
    )


class HandlerKnowledgeHealthCompute:
    """ONEX compute handler for deterministic knowledge health classification."""

    def handle(
        self, request: ModelKnowledgeHealthComputeRequest
    ) -> ModelKnowledgeHealthReport:
        return classify_knowledge_health(request)


__all__ = ["HandlerKnowledgeHealthCompute", "classify_knowledge_health"]
