"""Tests for knowledge health probe effect handler (injectable HTTP)."""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState
from omnimarket.nodes.node_knowledge_health_probe_effect.handlers.handler_knowledge_health_probe_effect import (
    HandlerKnowledgeHealthProbeEffect,
)
from omnimarket.nodes.node_knowledge_health_probe_effect.models.model_knowledge_health_probe_request import (
    ModelKnowledgeHealthProbeRequest,
)


async def _mock_get_repowise(url: str, timeout: float) -> tuple[int, dict]:
    return 200, {"index_age_days": 0.5, "indexed_file_count": 250}


async def _mock_get_qdrant(url: str, timeout: float) -> tuple[int, dict]:
    return 200, {"result": {"collections": [{"vectors_count": 1000}]}}


async def _mock_get_unavailable(url: str, timeout: float) -> tuple[int, dict]:
    import httpx

    raise httpx.ConnectError("connection refused")


@pytest.mark.unit
class TestHandlerKnowledgeHealthProbeEffect:
    @pytest.mark.asyncio
    async def test_repowise_fresh(self) -> None:
        handler = HandlerKnowledgeHealthProbeEffect(http_get_fn=_mock_get_repowise)
        request = ModelKnowledgeHealthProbeRequest(
            backends=("repowise",),
            repowise_url="http://localhost:9000",
        )
        result = await handler.handle(request)

        assert len(result.backend_probes) == 1
        probe = result.backend_probes[0]
        assert probe.backend_id == "repowise"
        assert probe.freshness_state == EnumKnowledgeFreshnessState.FRESH
        assert probe.entry_count == 250

    @pytest.mark.asyncio
    async def test_qdrant_fresh(self) -> None:
        handler = HandlerKnowledgeHealthProbeEffect(http_get_fn=_mock_get_qdrant)
        request = ModelKnowledgeHealthProbeRequest(
            backends=("qdrant",),
            qdrant_url="http://localhost:6333",
        )
        result = await handler.handle(request)

        probe = result.backend_probes[0]
        assert probe.backend_id == "qdrant"
        assert probe.freshness_state == EnumKnowledgeFreshnessState.FRESH
        assert probe.entry_count == 1000

    @pytest.mark.asyncio
    async def test_unavailable_backend_returns_unavailable_state(self) -> None:
        handler = HandlerKnowledgeHealthProbeEffect(http_get_fn=_mock_get_unavailable)
        request = ModelKnowledgeHealthProbeRequest(
            backends=("repowise",),
            repowise_url="http://localhost:9000",
        )
        result = await handler.handle(request)

        probe = result.backend_probes[0]
        assert probe.freshness_state == EnumKnowledgeFreshnessState.UNAVAILABLE
        assert probe.error is not None

    @pytest.mark.asyncio
    async def test_missing_url_returns_unknown(self) -> None:
        handler = HandlerKnowledgeHealthProbeEffect()
        request = ModelKnowledgeHealthProbeRequest(
            backends=("repowise",),
            repowise_url=None,
        )
        result = await handler.handle(request)

        probe = result.backend_probes[0]
        assert probe.freshness_state == EnumKnowledgeFreshnessState.UNKNOWN
        assert probe.error is not None

    @pytest.mark.asyncio
    async def test_multiple_backends_all_collected(self) -> None:
        handler = HandlerKnowledgeHealthProbeEffect()
        request = ModelKnowledgeHealthProbeRequest(
            backends=("repowise", "qdrant"),
            repowise_url=None,
            qdrant_url=None,
        )
        result = await handler.handle(request)

        backend_ids = {p.backend_id for p in result.backend_probes}
        assert backend_ids == {"repowise", "qdrant"}
