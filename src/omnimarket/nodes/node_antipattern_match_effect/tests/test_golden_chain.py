# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_antipattern_match_effect."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect import (
    HandlerAntipatternMatchEffect,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_request import (
    ModelAntipatternMatchRequest,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_response import (
    ModelAntipatternMatchResponse,
)


def _hit(*, score: float, name: str) -> MagicMock:
    hit = MagicMock()
    hit.score = score
    hit.payload = {
        "name": name,
        "severity": "ERROR",
        "enforcement": "blocking",
        "category": "architecture",
        "description": "Direct imports across runtime boundaries.",
        "rationale": "Boundary violations make dispatch behavior non-deterministic.",
        "source_ticket": "OMN-14272",
        "registry_version": "golden-chain",
    }
    return hit


def _qdrant(results: list[Any]) -> MagicMock:
    qdrant = MagicMock()
    qdrant.search.return_value = results
    return qdrant


def _http_client(embedding: list[float]) -> AsyncMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"embedding": embedding}]}

    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_golden_chain_antipattern_match_returns_ordered_typed_response() -> None:
    handler = HandlerAntipatternMatchEffect(
        qdrant_client=_qdrant(
            [
                _hit(score=0.82, name="lower_similarity_boundary_violation"),
                _hit(score=0.94, name="direct_runtime_boundary_import"),
                _hit(score=0.61, name="below_threshold"),
            ]
        )
    )

    with patch(
        "omnimarket.nodes.node_antipattern_match_effect.handlers.handler_antipattern_match_effect.httpx.AsyncClient",
        return_value=_http_client([0.1, 0.2, 0.3]),
    ):
        result = await handler.handle(
            ModelAntipatternMatchRequest(
                correlation_id="golden-chain-OMN-14272",
                code_text="from omnimarket.runtime import mutable_state",
                description="Handler reaches across the runtime boundary.",
                min_similarity=0.75,
                max_results=5,
                embedding_endpoint_override="http://embedding.local",
                freshness_decay_factor=0.0,
            )
        )

    assert isinstance(result, ModelAntipatternMatchResponse)
    assert result.correlation_id == "golden-chain-OMN-14272"
    assert result.query_text_used == (
        "Handler reaches across the runtime boundary.\n"
        "from omnimarket.runtime import mutable_state"
    )
    assert result.qdrant_collection == "onex_antipatterns"
    assert result.total_candidates_searched == 3
    assert [match.antipattern_name for match in result.matches] == [
        "direct_runtime_boundary_import",
        "lower_similarity_boundary_violation",
    ]
    assert all(match.similarity_score >= 0.75 for match in result.matches)
