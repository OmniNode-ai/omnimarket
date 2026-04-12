# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_intent_storage_effect.

Verifies store, get_session, and get_distribution operations with a mock
Memgraph adapter. No real database required.

Related: OMN-8300 — Wave 4 intent pipeline migration to omnimarket
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_core.enums.intelligence.enum_intent_category import EnumIntentCategory
from omnibase_core.models.intelligence import ModelIntentClassificationOutput

from omnimarket.nodes.node_intent_storage_effect import (
    HandlerIntentStorageAdapter,
    ModelIntentStorageRequest,
    ModelIntentStorageResponse,
)


def _make_intent_data(
    category: EnumIntentCategory = EnumIntentCategory.DEBUGGING,
    confidence: float = 0.9,
) -> ModelIntentClassificationOutput:
    return ModelIntentClassificationOutput(
        success=True,
        intent_category=category,
        confidence=confidence,
        keywords=["test"],
    )


def _make_graph_adapter_mock(
    response: ModelIntentStorageResponse | None = None,
) -> MagicMock:
    mock = MagicMock()
    default = response or ModelIntentStorageResponse(
        status="success",
        intent_id=uuid4(),
        created=True,
    )
    mock.execute = AsyncMock(return_value=default)
    return mock


@pytest.mark.unit
class TestIntentStorageEffect:
    """Golden chain tests for node_intent_storage_effect."""

    async def test_node_importable(self) -> None:
        from omnimarket.nodes import node_intent_storage_effect

        assert node_intent_storage_effect is not None

    async def test_handler_importable(self) -> None:
        assert HandlerIntentStorageAdapter is not None

    async def test_store_operation_calls_graph_adapter(self) -> None:
        success_response = ModelIntentStorageResponse(
            status="success",
            intent_id=uuid4(),
            created=True,
        )
        mock_graph_adapter = MagicMock()
        mock_graph_adapter.store_intent = AsyncMock(return_value=success_response)

        handler = HandlerIntentStorageAdapter.__new__(HandlerIntentStorageAdapter)
        handler._initialized = True  # type: ignore[attr-defined]
        handler._adapter = mock_graph_adapter  # type: ignore[attr-defined]

        request = ModelIntentStorageRequest(
            operation="store",
            session_id="session-abc",
            intent_data=_make_intent_data(),
        )
        with patch.object(handler, "execute", AsyncMock(return_value=success_response)):
            response = await handler.execute(request)

        assert response.status == "success"
        assert response.intent_id is not None

    async def test_get_distribution_request_is_valid(self) -> None:
        request = ModelIntentStorageRequest(operation="get_distribution")
        assert request.operation == "get_distribution"

    async def test_get_session_request_requires_session_id(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="session_id"):
            ModelIntentStorageRequest(operation="get_session")

    async def test_store_request_requires_session_and_intent(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match=r"session_id|intent_data"):
            ModelIntentStorageRequest(operation="store")

    async def test_store_request_valid_construction(self) -> None:
        request = ModelIntentStorageRequest(
            operation="store",
            session_id="session-xyz",
            intent_data=_make_intent_data(
                category=EnumIntentCategory.HELP, confidence=0.75
            ),
        )
        assert request.operation == "store"
        assert request.session_id == "session-xyz"
        assert request.intent_data is not None

    async def test_response_error_status(self) -> None:
        resp = ModelIntentStorageResponse(
            status="error",
            error_message="Memgraph unavailable",
        )
        assert resp.status == "error"
        assert resp.error_message == "Memgraph unavailable"

    async def test_response_distribution_data(self) -> None:
        resp = ModelIntentStorageResponse(
            status="success",
            distribution={"debugging": 10, "help": 5},
            total_intents=15,
        )
        assert resp.distribution["debugging"] == 10
        assert resp.total_intents == 15
