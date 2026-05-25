# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerAntipatternIndexEffect.

Unit tests: mocked Qdrant + embedding client; exercises real code paths.
Integration stub: @pytest.mark.integration — skipped unless .201 is available.

[OMN-11913, OMN-11909]
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_antipattern_index_effect.handlers.handler_antipattern_index_effect import (
    HandlerAntipatternIndexEffect,
    _build_entry_text,
    _entry_point_id,
)

# =============================================================================
# Minimal stubs for omnibase_core models (avoid cross-repo import in unit tests)
# =============================================================================


class _FakeExample:
    def __init__(
        self, kind: str = "bad", label: str = "example", code: str = "pass"
    ) -> None:
        self.kind = kind
        self.label = label
        self.code = code


class _FakeEntry:
    def __init__(
        self,
        name: str = "test_antipattern",
        vector_enabled: bool = True,
        description: str = "Test description.",
        rationale: str = "Test rationale.",
        examples: list[Any] | None = None,
        severity: str = "ERROR",
        enforcement: str = "blocking",
        category: str = "architecture",
        pattern_type: str = "semantic",
        source_ticket: str = "OMN-11913",
    ) -> None:
        self.name = name
        self.vector_enabled = vector_enabled
        self.description = description
        self.rationale = rationale
        self.examples = examples or []
        self.severity = severity
        self.enforcement = enforcement
        self.category = category
        self.pattern_type = pattern_type
        self.source_ticket = source_ticket


class _FakeRegistry:
    def __init__(
        self,
        version: str = "1.0.0",
        entries: list[Any] | None = None,
    ) -> None:
        self.version = version
        self.last_updated = datetime(2026, 5, 24, tzinfo=UTC)
        self.entries = entries or []


# =============================================================================
# Fixtures
# =============================================================================


def _make_mock_qdrant(collection: str = "onex_antipatterns") -> MagicMock:
    qdrant = MagicMock()
    qdrant.get_collections.return_value = MagicMock(
        collections=[MagicMock(name=collection)]
    )
    qdrant.retrieve.return_value = []  # not yet indexed
    qdrant.upsert = MagicMock()
    return qdrant


def _make_fake_registry(
    vector_entries: int = 2,
    non_vector_entries: int = 1,
    version: str = "1.0.0",
) -> _FakeRegistry:
    entries: list[Any] = []
    for i in range(vector_entries):
        entries.append(
            _FakeEntry(
                name=f"semantic_pattern_{i}",
                vector_enabled=True,
                description=f"Description for pattern {i}.",
                rationale=f"Rationale for pattern {i}.",
                examples=[
                    _FakeExample(kind="bad", label=f"Bad example {i}", code="x = 1")
                ],
            )
        )
    for i in range(non_vector_entries):
        entries.append(
            _FakeEntry(
                name=f"regex_pattern_{i}",
                vector_enabled=False,
                description=f"Regex pattern {i}.",
                rationale=f"Why regex {i}.",
                examples=[],
            )
        )
    return _FakeRegistry(version=version, entries=entries)


def _make_registry_loader(registry: _FakeRegistry) -> Any:
    """Return a callable that always returns the given registry."""
    return lambda _root: registry


# =============================================================================
# Unit tests: _entry_point_id
# =============================================================================


@pytest.mark.unit
class TestEntryPointId:
    def test_deterministic(self) -> None:
        assert _entry_point_id("foo", "1.0.0") == _entry_point_id("foo", "1.0.0")

    def test_different_name_different_id(self) -> None:
        assert _entry_point_id("foo", "1.0.0") != _entry_point_id("bar", "1.0.0")

    def test_different_version_different_id(self) -> None:
        assert _entry_point_id("foo", "1.0.0") != _entry_point_id("foo", "2.0.0")

    def test_positive(self) -> None:
        assert _entry_point_id("any_name", "1.0.0") >= 0


# =============================================================================
# Unit tests: _build_entry_text
# =============================================================================


@pytest.mark.unit
class TestBuildEntryText:
    def test_includes_description_and_rationale(self) -> None:
        entry = _FakeEntry(description="Desc text.", rationale="Rationale text.")
        text = _build_entry_text(entry)
        assert "Desc text." in text
        assert "Rationale text." in text

    def test_includes_example_label(self) -> None:
        entry = _FakeEntry(
            examples=[_FakeExample(kind="bad", label="Direct import", code="import x")]
        )
        text = _build_entry_text(entry)
        assert "Direct import" in text

    def test_no_examples_still_works(self) -> None:
        entry = _FakeEntry(examples=[])
        text = _build_entry_text(entry)
        assert len(text) > 0

    def test_empty_label_excluded(self) -> None:
        entry = _FakeEntry(examples=[_FakeExample(kind="bad", label="", code="pass")])
        text = _build_entry_text(entry)
        assert "bad:" not in text


# =============================================================================
# Unit tests: HandlerAntipatternIndexEffect.handle
# =============================================================================


def _make_mock_http_client(fake_embedding: list[float]) -> AsyncMock:
    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": fake_embedding}]}
    mock_http.post = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    return mock_http


@pytest.mark.unit
class TestHandlerAntipatternIndexEffect:
    @pytest.mark.asyncio
    async def test_upsert_called_for_each_vector_enabled_entry(self) -> None:
        registry = _make_fake_registry(vector_entries=2, non_vector_entries=1)
        qdrant = _make_mock_qdrant()
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternIndexEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_index_effect.handlers.handler_antipattern_index_effect.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-001",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
                registry_loader=_make_registry_loader(registry),
            )

        assert result.correlation_id == "test-001"
        assert result.indexed_count == 2
        # upsert called for 2 vector entries + 1 version metadata write
        assert qdrant.upsert.call_count == 3

    @pytest.mark.asyncio
    async def test_upsert_payload_contains_required_fields(self) -> None:
        registry = _make_fake_registry(vector_entries=1, non_vector_entries=0)
        qdrant = _make_mock_qdrant()
        fake_embedding = [0.2] * 4096
        handler = HandlerAntipatternIndexEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_index_effect.handlers.handler_antipattern_index_effect.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _make_mock_http_client(fake_embedding)

            await handler.handle(
                correlation_id="test-002",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
                registry_loader=_make_registry_loader(registry),
            )

        # First upsert call is the entry itself
        entry_upsert_call = qdrant.upsert.call_args_list[0]
        points = entry_upsert_call.kwargs["points"]
        assert len(points) == 1
        payload = points[0].payload
        assert "name" in payload
        assert "severity" in payload
        assert "enforcement" in payload
        assert "category" in payload
        assert "registry_version" in payload
        assert payload["registry_version"] == "1.0.0"
        assert points[0].vector == fake_embedding

    @pytest.mark.asyncio
    async def test_no_vector_entries_returns_zero_indexed(self) -> None:
        registry = _make_fake_registry(vector_entries=0, non_vector_entries=3)
        qdrant = _make_mock_qdrant()
        handler = HandlerAntipatternIndexEffect()

        result = await handler.handle(
            correlation_id="test-003",
            qdrant_client=qdrant,
            embedding_endpoint_override="http://test-embed:8100",
            registry_loader=_make_registry_loader(registry),
        )

        assert result.indexed_count == 0
        assert result.skipped_count == 3
        qdrant.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_embedding_url_raises(self) -> None:
        import os

        registry = _make_fake_registry(vector_entries=1)
        handler = HandlerAntipatternIndexEffect()

        backup = os.environ.pop("EMBEDDING_MODEL_URL", None)
        try:
            with pytest.raises(OSError, match="EMBEDDING_MODEL_URL"):
                await handler.handle(
                    correlation_id="test-004",
                    qdrant_client=_make_mock_qdrant(),
                    registry_loader=_make_registry_loader(registry),
                )
        finally:
            if backup is not None:
                os.environ["EMBEDDING_MODEL_URL"] = backup

    @pytest.mark.asyncio
    async def test_qdrant_none_host_missing_graceful_skip(self) -> None:
        import os

        registry = _make_fake_registry(vector_entries=2)
        handler = HandlerAntipatternIndexEffect()

        backup = os.environ.pop("QDRANT_HOST", None)
        try:
            result = await handler.handle(
                correlation_id="test-005",
                qdrant_client=None,
                embedding_endpoint_override="http://test-embed:8100",
                registry_loader=_make_registry_loader(registry),
            )
        finally:
            if backup is not None:
                os.environ["QDRANT_HOST"] = backup

        assert result.indexed_count == 0
        assert result.skipped_count == 0

    @pytest.mark.asyncio
    async def test_idempotency_no_op_when_version_already_indexed(self) -> None:
        registry = _make_fake_registry(vector_entries=2, version="1.0.0")
        qdrant = _make_mock_qdrant()
        qdrant.retrieve.return_value = [
            MagicMock(payload={"registry_version": "1.0.0", "_meta": True})
        ]
        handler = HandlerAntipatternIndexEffect()

        result = await handler.handle(
            correlation_id="test-006",
            qdrant_client=qdrant,
            embedding_endpoint_override="http://test-embed:8100",
            registry_loader=_make_registry_loader(registry),
        )

        assert result.was_no_op is True
        assert result.indexed_count == 0
        qdrant.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_reindex_bypasses_idempotency(self) -> None:
        registry = _make_fake_registry(vector_entries=1, version="1.0.0")
        qdrant = _make_mock_qdrant()
        qdrant.retrieve.return_value = [
            MagicMock(payload={"registry_version": "1.0.0", "_meta": True})
        ]
        fake_embedding = [0.3] * 4096
        handler = HandlerAntipatternIndexEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_index_effect.handlers.handler_antipattern_index_effect.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-007",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
                force_reindex=True,
                registry_loader=_make_registry_loader(registry),
            )

        assert result.was_no_op is False
        assert result.indexed_count == 1

    @pytest.mark.asyncio
    async def test_embedding_failure_increments_skipped(self) -> None:
        registry = _make_fake_registry(vector_entries=2, non_vector_entries=0)
        qdrant = _make_mock_qdrant()
        handler = HandlerAntipatternIndexEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_index_effect.handlers.handler_antipattern_index_effect.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            result = await handler.handle(
                correlation_id="test-008",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
                registry_loader=_make_registry_loader(registry),
            )

        assert result.indexed_count == 0
        assert result.skipped_count == 2

    @pytest.mark.asyncio
    async def test_correlation_id_propagated(self) -> None:
        registry = _make_fake_registry(vector_entries=0)
        qdrant = _make_mock_qdrant()
        handler = HandlerAntipatternIndexEffect()

        result = await handler.handle(
            correlation_id="unique-xyz-789",
            qdrant_client=qdrant,
            embedding_endpoint_override="http://test-embed:8100",
            registry_loader=_make_registry_loader(registry),
        )

        assert result.correlation_id == "unique-xyz-789"


# =============================================================================
# Integration stub — requires .201 to be reachable
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_collection_exists_after_run() -> None:
    """Verify onex_antipatterns collection exists in real Qdrant after indexing."""
    import os

    endpoint = os.environ.get("EMBEDDING_MODEL_URL")
    if not endpoint:
        pytest.skip("EMBEDDING_MODEL_URL not set — skipping integration probe")

    qdrant_host = os.environ.get("QDRANT_HOST")
    if not qdrant_host:
        pytest.skip("QDRANT_HOST not set — skipping integration probe")

    try:
        from qdrant_client import QdrantClient

        qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
        client = QdrantClient(host=qdrant_host, port=qdrant_port)
        client.get_collections()
    except Exception as exc:
        pytest.skip(f"Qdrant unreachable: {exc}")

    handler = HandlerAntipatternIndexEffect()
    result = await handler.handle(
        correlation_id="integration-probe-001",
        qdrant_client=client,
        embedding_endpoint_override=endpoint,
        force_reindex=True,
    )

    collections = [c.name for c in client.get_collections().collections]
    assert "onex_antipatterns" in collections, "Collection must exist after run"
    assert result.registry_version, "registry_version must be non-empty"
