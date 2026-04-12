# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_intent_query_effect.

Verifies distribution analysis, session retrieval, and recent intent queries
with a mock Memgraph adapter. No real database required.

Related: OMN-8300 — Wave 4 intent pipeline migration to omnimarket
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from omnibase_core.models.events import (
    ModelIntentQueryRequestedEvent,
    ModelIntentQueryResponseEvent,
)

from omnimarket.nodes.node_intent_query_effect.handlers import HandlerIntentQuery
from omnimarket.nodes.node_intent_query_effect.models import (
    ModelHandlerIntentQueryConfig,
)


def _make_container() -> MagicMock:
    return MagicMock()


def _make_handler_with_mock_adapter() -> tuple[HandlerIntentQuery, MagicMock]:
    container = _make_container()
    handler = HandlerIntentQuery(container)
    mock_adapter = MagicMock()
    handler._adapter = mock_adapter
    handler._initialized = True
    handler._config = ModelHandlerIntentQueryConfig()
    return handler, mock_adapter


@pytest.mark.unit
class TestIntentQueryEffect:
    """Golden chain tests for node_intent_query_effect."""

    async def test_node_importable(self) -> None:
        from omnimarket.nodes import node_intent_query_effect

        assert node_intent_query_effect is not None

    async def test_handler_importable(self) -> None:
        assert HandlerIntentQuery is not None

    async def test_distribution_query_returns_distribution(self) -> None:
        handler, mock_adapter = _make_handler_with_mock_adapter()

        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.distribution = {"debugging": 7, "question": 3}
        mock_result.error_message = None
        mock_adapter.get_intent_distribution = AsyncMock(return_value=mock_result)

        request = ModelIntentQueryRequestedEvent(
            query_id=str(uuid4()),
            query_type="distribution",
            time_range_hours=24,
            min_confidence=0.0,
            limit=100,
        )
        response = await handler.execute(request)

        assert response.status == "success"
        assert response.distribution is not None
        assert response.distribution.get("debugging") == 7
        mock_adapter.get_intent_distribution.assert_awaited_once()

    async def test_session_query_returns_intents(self) -> None:
        handler, mock_adapter = _make_handler_with_mock_adapter()

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.intents = []
        mock_result.error_message = None
        mock_adapter.get_session_intents = AsyncMock(return_value=mock_result)

        request = ModelIntentQueryRequestedEvent(
            query_id=str(uuid4()),
            query_type="session",
            session_ref="session-abc",
            min_confidence=0.0,
            limit=50,
        )
        response = await handler.execute(request)

        assert response.status in ("success", "no_results")
        mock_adapter.get_session_intents.assert_awaited_once()

    async def test_session_query_without_session_ref_returns_error(self) -> None:
        handler, _ = _make_handler_with_mock_adapter()

        request = ModelIntentQueryRequestedEvent(
            query_id=str(uuid4()),
            query_type="session",
            min_confidence=0.0,
            limit=50,
        )
        response = await handler.execute(request)

        assert response.status == "error"
        assert response.error_message is not None

    async def test_uninitialized_handler_returns_error(self) -> None:
        container = _make_container()
        handler = HandlerIntentQuery(container)

        request = ModelIntentQueryRequestedEvent(
            query_id=str(uuid4()),
            query_type="distribution",
            time_range_hours=24,
            min_confidence=0.0,
            limit=100,
        )
        response = await handler.execute(request)

        assert isinstance(response, ModelIntentQueryResponseEvent)
        assert response.status == "error"
        assert "not initialized" in (response.error_message or "").lower()
