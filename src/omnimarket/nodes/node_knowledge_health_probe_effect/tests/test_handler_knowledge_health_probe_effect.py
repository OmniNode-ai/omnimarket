# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

import pytest

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState
from omnimarket.nodes.node_knowledge_health_probe_effect.handlers.handler_knowledge_health_probe_effect import (
    HandlerKnowledgeHealthProbeEffect,
)
from omnimarket.nodes.node_knowledge_health_probe_effect.models.model_knowledge_health_probe_request import (
    ModelKnowledgeHealthProbeRequest,
)


async def _fake_get(
    url: str,
    timeout: float,
) -> tuple[int, dict[str, object]]:
    assert url.endswith("/api/health")
    assert timeout > 0
    return 200, {"index_age_days": 0, "indexed_file_count": 7}


@pytest.mark.asyncio
async def test_handler_collects_repowise_probe() -> None:
    result = await HandlerKnowledgeHealthProbeEffect(http_get_fn=_fake_get).handle(
        ModelKnowledgeHealthProbeRequest(
            backends=("repowise",),
            repowise_url="https://repowise.example",
        )
    )

    assert result.backend_probes[0].backend_id == "repowise"
    assert result.backend_probes[0].freshness_state is EnumKnowledgeFreshnessState.FRESH
