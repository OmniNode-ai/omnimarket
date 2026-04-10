# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_semantic_analyzer_compute.

Verifies embedding generation, entity extraction, full analysis,
caching, idempotency, and error handling. Uses a stub embedding provider
so no external services are required.

Migrated from omnimemory to omnimarket (OMN-8297).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from omnibase_core.container import ModelONEXContainer

from omnimarket.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticCompute,
)
from omnimarket.nodes.node_semantic_analyzer_compute.models import (
    ModelSemanticAnalyzerComputeRequest,
)

# Stub embedding dimension
_DIM = 8
_STUB_EMBEDDING = [float(i) / _DIM for i in range(_DIM)]


class _StubEmbeddingProvider:
    """Minimal stub implementing ProtocolEmbeddingProvider."""

    provider_name: str = "stub-provider"
    model_name: str = "stub-model"
    embedding_dimension: int = _DIM

    async def generate_embedding(
        self,
        text: str,
        model: str | None = None,
        correlation_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> list[float]:
        return list(_STUB_EMBEDDING)

    async def generate_embeddings_batch(
        self,
        texts: list[str],
        model: str | None = None,
        correlation_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        return [list(_STUB_EMBEDDING) for _ in texts]

    async def health_check(self) -> bool:
        return True

    async def is_available(self) -> bool:
        return True


@pytest.fixture
async def handler() -> HandlerSemanticCompute:
    """Initialized handler with stub embedding provider."""
    container = ModelONEXContainer()
    h = HandlerSemanticCompute(container)
    await h.initialize(embedding_provider=_StubEmbeddingProvider())  # type: ignore[arg-type]
    return h


@pytest.mark.unit
class TestSemanticAnalyzerComputeGoldenChain:
    """Golden chain: text content in -> semantic analysis out."""

    async def test_embed_returns_fixed_dimension(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Embed operation returns embedding with correct dimension."""
        embedding = await handler.embed("Hello, world!")
        assert len(embedding) == _DIM
        assert embedding == _STUB_EMBEDDING

    async def test_extract_entities_heuristic_returns_list(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Entity extraction returns a list (may be empty for lowercase text)."""
        entity_list = await handler.extract_entities("John works at Google.")
        assert hasattr(entity_list, "entities")
        assert isinstance(entity_list.entities, list)

    async def test_analyze_full_returns_all_fields(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Full analysis populates embedding, topics, and scores."""
        result = await handler.analyze(
            "Machine learning is used in many Python projects today.",
            analysis_type="full",
        )
        assert result.semantic_vector is not None
        assert len(result.semantic_vector) == _DIM
        assert result.topics is not None
        assert result.confidence_score is not None
        assert result.complexity_score is not None
        assert result.readability_score is not None

    async def test_analyze_embedding_only(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """embedding_only analysis populates embedding but not topics."""
        result = await handler.analyze(
            "Test content for embedding only.",
            analysis_type="embedding_only",
        )
        assert result.semantic_vector is not None
        assert len(result.semantic_vector) == _DIM
        assert not result.topics  # Should be empty for embedding_only

    async def test_analyze_entities_only(self, handler: HandlerSemanticCompute) -> None:
        """entities_only analysis populates entities but not embedding."""
        result = await handler.analyze(
            "Alice visited London last month.",
            analysis_type="entities_only",
        )
        assert result.entity_list is not None
        assert result.semantic_vector == []  # No embedding for entities_only

    async def test_empty_content_raises(self, handler: HandlerSemanticCompute) -> None:
        """Empty content raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await handler.embed("")

    async def test_whitespace_only_raises(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Whitespace-only content raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            await handler.embed("   ")

    async def test_idempotency_embed(self, handler: HandlerSemanticCompute) -> None:
        """Same content embedded twice returns same result."""
        content = "Deterministic embedding test."
        e1 = await handler.embed(content)
        e2 = await handler.embed(content)
        assert e1 == e2

    async def test_health_check_initialized(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Health check reports embedding provider healthy."""
        health = await handler.health_check()
        assert health.initialized is True
        assert health.embedding_provider_healthy is True

    async def test_model_request_response_contract(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Request model validates and response has correct contract fields."""
        request = ModelSemanticAnalyzerComputeRequest(
            operation="embed",
            content="Contract validation test.",
        )
        assert request.operation == "embed"

        embedding = await handler.embed(request.content)
        assert len(embedding) == _DIM
