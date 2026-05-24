# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerAntipatternMatchEffect.

Unit tests: mocked Qdrant + embedding client; exercises real code paths.
Integration stub: @pytest.mark.integration — skipped unless .201 is available.

[OMN-11919, OMN-11909]
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect import (
    HandlerAntipatternMatchEffect,
    _apply_freshness_boost,
    _build_explanation,
    _build_query_text,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match import (
    ModelAntipatternMatch,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_response import (
    ModelAntipatternMatchResponse,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_mock_qdrant(results: list[Any] | None = None) -> MagicMock:
    qdrant = MagicMock()
    qdrant.get_collections.return_value = MagicMock(
        collections=[MagicMock(name="onex_antipatterns")]
    )
    qdrant.search.return_value = results or []
    return qdrant


def _make_qdrant_hit(
    score: float = 0.9,
    name: str = "test_antipattern",
    severity: str = "ERROR",
    enforcement: str = "blocking",
    category: str = "architecture",
    description: str = "Test description.",
    rationale: str = "Test rationale.",
    source_ticket: str = "OMN-9999",
    registry_version: str = "1.0.0",
    discovered_at: str | None = None,
) -> MagicMock:
    hit = MagicMock()
    hit.score = score
    hit.payload = {
        "name": name,
        "severity": severity,
        "enforcement": enforcement,
        "category": category,
        "description": description,
        "rationale": rationale,
        "source_ticket": source_ticket,
        "registry_version": registry_version,
    }
    if discovered_at is not None:
        hit.payload["discovered_at"] = discovered_at
    return hit


def _make_mock_http_client(fake_embedding: list[float]) -> AsyncMock:
    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": fake_embedding}]}
    mock_http.post = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    return mock_http


# =============================================================================
# Unit tests: helper functions
# =============================================================================


@pytest.mark.unit
class TestBuildQueryText:
    def test_code_text_used_directly(self) -> None:
        text = _build_query_text(code_text="def foo(): pass", description=None)
        assert text == "def foo(): pass"

    def test_description_used_when_no_code(self) -> None:
        text = _build_query_text(code_text=None, description="Uses hardcoded path")
        assert text == "Uses hardcoded path"

    def test_both_combined(self) -> None:
        text = _build_query_text(
            code_text="ENDPOINT = 'http://...'",
            description="Hardcoded endpoint",
        )
        assert "ENDPOINT" in text
        assert "Hardcoded endpoint" in text

    def test_both_none_raises(self) -> None:
        with pytest.raises(ValueError, match=r"code_text.*description"):
            _build_query_text(code_text=None, description=None)


@pytest.mark.unit
class TestApplyFreshnessBoost:
    def test_zero_decay_factor_no_change(self) -> None:
        score = _apply_freshness_boost(
            base_score=0.85,
            discovered_at=None,
            decay_factor=0.0,
        )
        assert score == pytest.approx(0.85)

    def test_recent_entry_gets_boost(self) -> None:
        score = _apply_freshness_boost(
            base_score=0.85,
            discovered_at="2026-05-24T00:00:00Z",
            decay_factor=0.05,
        )
        assert score >= 0.85

    def test_old_entry_no_significant_boost(self) -> None:
        score_old = _apply_freshness_boost(
            base_score=0.85,
            discovered_at="2020-01-01T00:00:00Z",
            decay_factor=0.05,
        )
        score_recent = _apply_freshness_boost(
            base_score=0.85,
            discovered_at="2026-05-24T00:00:00Z",
            decay_factor=0.05,
        )
        assert score_recent > score_old

    def test_none_discovered_at_returns_base(self) -> None:
        score = _apply_freshness_boost(
            base_score=0.80,
            discovered_at=None,
            decay_factor=0.1,
        )
        assert score == pytest.approx(0.80)


@pytest.mark.unit
class TestBuildExplanation:
    def test_returns_nonempty_string(self) -> None:
        expl = _build_explanation(
            query_text="import x directly",
            antipattern_name="direct_import_violation",
            description="Direct imports across layer boundaries.",
        )
        assert len(expl) > 0

    def test_contains_antipattern_name(self) -> None:
        expl = _build_explanation(
            query_text="some code",
            antipattern_name="my_antipattern",
            description="Bad pattern.",
        )
        assert "my_antipattern" in expl


# =============================================================================
# Unit tests: HandlerAntipatternMatchEffect.handle
# =============================================================================


@pytest.mark.unit
class TestHandlerAntipatternMatchEffect:
    @pytest.mark.asyncio
    async def test_returns_matches_above_threshold(self) -> None:
        hits = [
            _make_qdrant_hit(score=0.92, name="antipattern_a"),
            _make_qdrant_hit(score=0.80, name="antipattern_b"),
            _make_qdrant_hit(score=0.60, name="antipattern_c"),  # below threshold
        ]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-001",
                code_text="def foo(): pass",
                min_similarity=0.75,
                max_results=5,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert isinstance(result, ModelAntipatternMatchResponse)
        assert result.correlation_id == "test-001"
        assert len(result.matches) == 2
        names = {m.antipattern_name for m in result.matches}
        assert "antipattern_a" in names
        assert "antipattern_b" in names
        assert "antipattern_c" not in names

    @pytest.mark.asyncio
    async def test_matches_ordered_by_adjusted_score_descending(self) -> None:
        hits = [
            _make_qdrant_hit(score=0.80, name="lower"),
            _make_qdrant_hit(score=0.92, name="higher"),
        ]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-002",
                code_text="some code",
                min_similarity=0.75,
                max_results=5,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.matches[0].antipattern_name == "higher"
        assert result.matches[1].antipattern_name == "lower"

    @pytest.mark.asyncio
    async def test_max_results_limits_output(self) -> None:
        hits = [_make_qdrant_hit(score=0.90, name=f"ap_{i}") for i in range(10)]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-003",
                code_text="code",
                min_similarity=0.0,
                max_results=3,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert len(result.matches) <= 3

    @pytest.mark.asyncio
    async def test_search_called_with_correct_params(self) -> None:
        qdrant = _make_mock_qdrant(results=[])
        fake_embedding = [0.5] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            await handler.handle(
                correlation_id="test-004",
                code_text="test code",
                min_similarity=0.8,
                max_results=7,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        qdrant.search.assert_called_once()
        call_kwargs = qdrant.search.call_args.kwargs
        assert call_kwargs["limit"] == 7
        assert call_kwargs["query_vector"] == fake_embedding

    @pytest.mark.asyncio
    async def test_match_has_required_fields(self) -> None:
        hits = [_make_qdrant_hit(score=0.88, name="test_ap")]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-005",
                code_text="code",
                min_similarity=0.75,
                max_results=5,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert len(result.matches) == 1
        match = result.matches[0]
        assert isinstance(match, ModelAntipatternMatch)
        assert match.similarity_score == pytest.approx(0.88)
        assert match.antipattern_name == "test_ap"
        assert match.severity == "ERROR"
        assert match.enforcement == "blocking"
        assert match.category == "architecture"
        assert len(match.explanation) > 0

    @pytest.mark.asyncio
    async def test_missing_embedding_url_raises(self) -> None:
        import os

        handler = HandlerAntipatternMatchEffect()
        backup = os.environ.pop("EMBEDDING_MODEL_URL", None)
        try:
            with pytest.raises(OSError, match="EMBEDDING_MODEL_URL"):
                await handler.handle(
                    correlation_id="test-006",
                    code_text="some code",
                    qdrant_client=_make_mock_qdrant(),
                )
        finally:
            if backup is not None:
                os.environ["EMBEDDING_MODEL_URL"] = backup

    @pytest.mark.asyncio
    async def test_both_code_and_description_none_raises(self) -> None:
        handler = HandlerAntipatternMatchEffect()
        with pytest.raises(ValueError, match=r"code_text.*description"):
            await handler.handle(
                correlation_id="test-007",
                qdrant_client=_make_mock_qdrant(),
                embedding_endpoint_override="http://test-embed:8100",
            )

    @pytest.mark.asyncio
    async def test_qdrant_unavailable_returns_empty_matches(self) -> None:
        import os

        handler = HandlerAntipatternMatchEffect()
        backup = os.environ.pop("QDRANT_HOST", None)
        try:
            result = await handler.handle(
                correlation_id="test-008",
                code_text="some code",
                qdrant_client=None,
                embedding_endpoint_override="http://test-embed:8100",
            )
        finally:
            if backup is not None:
                os.environ["QDRANT_HOST"] = backup

        assert result.matches == ()
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_empty_matches(self) -> None:
        qdrant = _make_mock_qdrant(results=[])
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_http

            result = await handler.handle(
                correlation_id="test-009",
                code_text="code",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.matches == ()
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_freshness_decay_reranks_recent_antipattern(self) -> None:
        hits = [
            _make_qdrant_hit(
                score=0.90,
                name="old_ap",
                discovered_at="2020-01-01T00:00:00Z",
            ),
            _make_qdrant_hit(
                score=0.85,
                name="recent_ap",
                discovered_at="2026-05-24T00:00:00Z",
            ),
        ]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-010",
                code_text="code",
                min_similarity=0.75,
                max_results=5,
                freshness_decay_factor=0.20,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        # recent_ap (0.85 base + big freshness boost) should outrank old_ap (0.90 base + tiny boost)
        assert result.matches[0].antipattern_name == "recent_ap"

    @pytest.mark.asyncio
    async def test_zero_freshness_decay_preserves_original_order(self) -> None:
        hits = [
            _make_qdrant_hit(
                score=0.90, name="higher", discovered_at="2026-05-24T00:00:00Z"
            ),
            _make_qdrant_hit(
                score=0.80, name="lower", discovered_at="2020-01-01T00:00:00Z"
            ),
        ]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-011",
                code_text="code",
                min_similarity=0.75,
                max_results=5,
                freshness_decay_factor=0.0,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.matches[0].antipattern_name == "higher"
        assert result.matches[1].antipattern_name == "lower"

    @pytest.mark.asyncio
    async def test_correlation_id_propagated(self) -> None:
        qdrant = _make_mock_qdrant(results=[])
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="unique-corr-abc",
                code_text="code",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.correlation_id == "unique-corr-abc"

    @pytest.mark.asyncio
    async def test_query_text_used_populated_in_response(self) -> None:
        qdrant = _make_mock_qdrant(results=[])
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-012",
                code_text="def my_func(): pass",
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert "my_func" in result.query_text_used

    @pytest.mark.asyncio
    async def test_description_only_query(self) -> None:
        hits = [_make_qdrant_hit(score=0.88, name="desc_match")]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-013",
                description="Hardcoded connection string in YAML",
                min_similarity=0.75,
                max_results=5,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert len(result.matches) == 1
        assert result.matches[0].antipattern_name == "desc_match"

    @pytest.mark.asyncio
    async def test_total_candidates_searched_reflects_qdrant_results(self) -> None:
        hits = [_make_qdrant_hit(score=0.9), _make_qdrant_hit(score=0.7)]
        qdrant = _make_mock_qdrant(results=hits)
        fake_embedding = [0.1] * 4096
        handler = HandlerAntipatternMatchEffect()

        with patch(
            "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient"
        ) as mock_cls:
            mock_cls.return_value = _make_mock_http_client(fake_embedding)

            result = await handler.handle(
                correlation_id="test-014",
                code_text="code",
                min_similarity=0.75,
                max_results=5,
                qdrant_client=qdrant,
                embedding_endpoint_override="http://test-embed:8100",
            )

        assert result.total_candidates_searched == 2


# =============================================================================
# Integration stub — requires .201 to be reachable
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_match_returns_results() -> None:
    """Verify real Qdrant search returns matching antipatterns."""
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
        collections = [c.name for c in client.get_collections().collections]
        if "onex_antipatterns" not in collections:
            pytest.skip("onex_antipatterns collection not found — run indexer first")
    except Exception as exc:
        pytest.skip(f"Qdrant unreachable: {exc}")

    handler = HandlerAntipatternMatchEffect()
    result = await handler.handle(
        correlation_id="integration-match-001",
        code_text="import omnibase_core.models directly from handler",
        qdrant_client=client,
        embedding_endpoint_override=endpoint,
        min_similarity=0.5,
        max_results=5,
    )

    assert result.correlation_id == "integration-match-001"
    assert result.qdrant_collection != ""
