# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState
from omnimarket.events.knowledge_health import ModelKnowledgeBackendProbe
from omnimarket.nodes.node_knowledge_health_compute.handlers.handler_knowledge_health_compute import (
    HandlerKnowledgeHealthCompute,
)
from omnimarket.nodes.node_knowledge_health_compute.models.model_knowledge_health_compute_request import (
    ModelKnowledgeHealthComputeRequest,
)


def test_handler_classifies_probe_results() -> None:
    result = HandlerKnowledgeHealthCompute().handle(
        ModelKnowledgeHealthComputeRequest(
            backend_probes=(
                ModelKnowledgeBackendProbe(
                    backend_id="repowise",
                    freshness_state=EnumKnowledgeFreshnessState.FRESH,
                    entry_count=3,
                ),
            )
        )
    )

    assert result.overall_status == "healthy"
    assert result.backend_statuses[0].backend_id == "repowise"
