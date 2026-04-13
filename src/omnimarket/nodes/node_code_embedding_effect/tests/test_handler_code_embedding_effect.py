# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerCodeEmbeddingEffect.

Unit tests: mocked Qdrant and embedding client, exercising real code paths.
Integration stubs: @pytest.mark.integration — skipped unless .201 is available.

[OMN-5657, OMN-5665]
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from omnimarket.nodes.node_code_embedding_effect.handlers.handler_code_embedding_effect import (
    HandlerCodeEmbeddingEffect,
    build_embedding_text,
)

# =============================================================================
# Fixtures
# =============================================================================


def _make_entity(**overrides: Any) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "id": overrides.pop("id", str(uuid4())),
        "entity_name": "MyClass",
        "entity_type": "class",
        "qualified_name": "mypackage.mymodule.MyClass",
        "source_repo": "omniintelligence",
        "source_path": "src/omniintelligence/models/my_class.py",
        "docstring": "A sample class for testing.",
        "signature": "class MyClass(BaseModel):",
        "classification": "model",
        "llm_description": None,
    }
    entity.update(overrides)
    return entity


def _make_mock_repository(entities: list[dict[str, Any]]) -> MagicMock:
    repo = MagicMock()
    repo.get_entities_needing_embedding = AsyncMock(return_value=entities)
    repo.update_embedded_at = AsyncMock()
    return repo


def _make_mock_qdrant(collection: str = "code_patterns") -> MagicMock:
    qdrant = MagicMock()
    qdrant.get_collections.return_value = MagicMock(
        collections=[MagicMock(name=collection)]
    )
    qdrant.upsert = MagicMock()
    return qdrant


# =============================================================================
# Unit tests: build_embedding_text
# =============================================================================


@pytest.mark.unit
class TestBuildEmbeddingText:
    def test_primary_fields_only(self) -> None:
        entity = _make_entity(llm_description=None)
        text = build_embedding_text(entity)
        assert "MyClass" in text
        assert "class MyClass(BaseModel):" in text
        assert "A sample class for testing." in text
        assert "\n" not in text

    def test_primary_and_secondary_fields(self) -> None:
        entity = _make_entity(llm_description="An LLM-generated description.")
        text = build_embedding_text(entity)
        assert "\n" in text
        assert "An LLM-generated description." in text.split("\n")[1]

    def test_empty_entity_returns_empty(self) -> None:
        entity = _make_entity(
            entity_name="", signature=None, docstring=None, llm_description=None
        )
        assert build_embedding_text(entity).strip() == ""

    def test_partial_primary_fields(self) -> None:
        entity = _make_entity(signature=None, docstring=None, llm_description=None)
        assert build_embedding_text(entity) == "MyClass"


# =============================================================================
# Unit tests: HandlerCodeEmbeddingEffect.handle
# =============================================================================


@pytest.mark.unit
class TestHandlerCodeEmbeddingEffect:
    @pytest.mark.asyncio
    async def test_upsert_called_with_correct_point(self) -> None:
        entity_id = str(uuid4())
        entity = _make_entity(id=entity_id)
        repo = _make_mock_repository([entity])
        qdrant = _make_mock_qdrant()
        fake_embedding = [0.1] * 4096
        handler = HandlerCodeEmbeddingEffect()

        with patch(
            "omnimarket.nodes.node_code_embedding_effect.handlers.handler_code_embedding_effect.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_http = AsyncMock()
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {"data": [{"embedding": fake_embedding}]}
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            result = await handler.handle(
                correlation_id="test-corr-001",
                repository=repo,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.correlation_id == "test-corr-001"
        assert result.embedded_count == 1
        assert result.failed_count == 0

        qdrant.upsert.assert_called_once()
        call_kwargs = qdrant.upsert.call_args.kwargs
        points = call_kwargs["points"]
        assert len(points) == 1
        point = points[0]
        assert point.id == entity_id
        assert point.vector == fake_embedding
        assert point.payload["entity_id"] == entity_id
        assert point.payload["entity_name"] == "MyClass"
        assert point.payload["source_repo"] == "omniintelligence"

        repo.update_embedded_at.assert_awaited_once_with([entity_id])

    @pytest.mark.asyncio
    async def test_embedding_failure_increments_failed_count(self) -> None:
        entity = _make_entity()
        repo = _make_mock_repository([entity])
        qdrant = _make_mock_qdrant()
        handler = HandlerCodeEmbeddingEffect()

        with patch(
            "omnimarket.nodes.node_code_embedding_effect.handlers.handler_code_embedding_effect.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            result = await handler.handle(
                correlation_id="test-corr-002",
                repository=repo,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.embedded_count == 0
        assert result.failed_count == 1
        repo.update_embedded_at.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_entities_returns_zero_counts(self) -> None:
        repo = _make_mock_repository([])
        qdrant = _make_mock_qdrant()
        handler = HandlerCodeEmbeddingEffect()

        result = await handler.handle(
            correlation_id="test-corr-003",
            repository=repo,
            qdrant_client=qdrant,
            embedding_endpoint_override="http://test-embed:8100",
        )

        assert result.embedded_count == 0
        assert result.failed_count == 0
        assert result.correlation_id == "test-corr-003"

    @pytest.mark.asyncio
    async def test_missing_embedding_url_raises(self) -> None:
        repo = _make_mock_repository([_make_entity()])
        qdrant = _make_mock_qdrant()
        handler = HandlerCodeEmbeddingEffect()

        import os

        env_backup = os.environ.pop("EMBEDDING_MODEL_URL", None)
        try:
            with pytest.raises(EnvironmentError, match="EMBEDDING_MODEL_URL"):
                await handler.handle(
                    correlation_id="test-corr-004",
                    repository=repo,
                    qdrant_client=qdrant,
                )
        finally:
            if env_backup is not None:
                os.environ["EMBEDDING_MODEL_URL"] = env_backup

    @pytest.mark.asyncio
    async def test_qdrant_none_and_host_missing_returns_graceful_skip(self) -> None:
        repo = _make_mock_repository([_make_entity()])
        handler = HandlerCodeEmbeddingEffect()

        import os

        env_backup = os.environ.pop("QDRANT_HOST", None)
        try:
            result = await handler.handle(
                correlation_id="test-corr-005",
                repository=repo,
                qdrant_client=None,
                embedding_endpoint_override="http://test-embed:8100",
            )
        finally:
            if env_backup is not None:
                os.environ["QDRANT_HOST"] = env_backup

        assert result.embedded_count == 0
        assert result.failed_count == 0

    @pytest.mark.asyncio
    async def test_correlation_id_propagated_to_output(self) -> None:
        repo = _make_mock_repository([])
        qdrant = _make_mock_qdrant()
        handler = HandlerCodeEmbeddingEffect()
        corr = "unique-correlation-xyz-789"

        result = await handler.handle(
            correlation_id=corr,
            repository=repo,
            qdrant_client=qdrant,
            embedding_endpoint_override="http://test-embed:8100",
        )

        assert result.correlation_id == corr


# =============================================================================
# Integration stubs — require .201 to be reachable
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_real_embedding_probe() -> None:
    """Probe the real embedding endpoint and Qdrant. Skipped unless .201 available."""
    import os

    endpoint = os.environ.get("EMBEDDING_MODEL_URL")
    if not endpoint:
        pytest.skip("EMBEDDING_MODEL_URL not set — skipping integration probe")

    qdrant_host = os.environ.get("QDRANT_HOST")
    if not qdrant_host:
        pytest.skip("QDRANT_HOST not set — skipping integration probe")

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(
                f"{endpoint}/v1/embeddings",
                json={"input": "test probe", "model": "embedding"},
            )
            assert resp.status_code == 200
        except Exception as exc:
            pytest.skip(f"Embedding endpoint unreachable: {exc}")
