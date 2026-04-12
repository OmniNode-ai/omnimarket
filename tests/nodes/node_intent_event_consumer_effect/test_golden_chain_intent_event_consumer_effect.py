# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_intent_event_consumer_effect.

Verifies event consumption routing to storage adapter with a mock bus and
mock Memgraph adapter. No real Kafka or database required.

Related: OMN-8300 — Wave 4 intent pipeline migration to omnimarket
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_intent_event_consumer_effect import (
    HandlerIntentEventConsumer,
    ModelIntentEventConsumerConfig,
    ModelIntentEventConsumerHealth,
)
from omnimarket.nodes.node_intent_storage_effect import (
    ModelIntentStorageResponse,
)


def _make_storage_adapter(
    response: ModelIntentStorageResponse | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    mock = MagicMock()
    if side_effect:
        mock.execute = AsyncMock(side_effect=side_effect)
    else:
        default = response or ModelIntentStorageResponse(
            status="success",
            intent_id=uuid4(),
            created=True,
        )
        mock.execute = AsyncMock(return_value=default)
    return mock


def _make_valid_message(
    session_id: str = "test-session",
    intent_category: str = "debugging",
    confidence: float = 0.85,
) -> dict[str, object]:
    return {
        "event_type": "IntentClassified",
        "session_id": session_id,
        "correlation_id": uuid4(),
        "intent_category": intent_category,
        "confidence": confidence,
        "keywords": ["test"],
        "emitted_at": datetime.now(UTC),
    }


@pytest.mark.unit
class TestIntentEventConsumerEffect:
    """Golden chain tests for node_intent_event_consumer_effect."""

    async def test_node_importable(self) -> None:
        from omnimarket.nodes import node_intent_event_consumer_effect

        assert node_intent_event_consumer_effect is not None

    async def test_handler_importable(self) -> None:
        assert HandlerIntentEventConsumer is not None

    async def test_valid_event_routes_to_storage(self) -> None:
        storage = _make_storage_adapter()
        config = ModelIntentEventConsumerConfig()
        consumer = HandlerIntentEventConsumer(config=config, storage_adapter=storage)

        message = _make_valid_message()
        await consumer._handle_message(message)

        storage.execute.assert_awaited_once()

    async def test_storage_error_increments_failed_count(self) -> None:
        error_response = ModelIntentStorageResponse(
            status="error",
            error_message="Memgraph write failed",
        )
        storage = _make_storage_adapter(response=error_response)
        config = ModelIntentEventConsumerConfig()
        consumer = HandlerIntentEventConsumer(config=config, storage_adapter=storage)

        message = _make_valid_message()
        await consumer._handle_message(message)

        assert consumer._messages_failed >= 1

    async def test_health_check_uninitialized_returns_unhealthy(self) -> None:
        storage = _make_storage_adapter()
        config = ModelIntentEventConsumerConfig()
        consumer = HandlerIntentEventConsumer(config=config, storage_adapter=storage)

        health = await consumer.health_check()
        assert isinstance(health, ModelIntentEventConsumerHealth)
        assert health.is_healthy is False

    async def test_invalid_message_does_not_raise(self) -> None:
        storage = _make_storage_adapter()
        config = ModelIntentEventConsumerConfig()
        consumer = HandlerIntentEventConsumer(config=config, storage_adapter=storage)

        bad_message: dict[str, object] = {"not_a_valid": "event"}
        # Should not raise — invalid messages are routed to DLQ or counted as failed
        await consumer._handle_message(bad_message)
        storage.execute.assert_not_awaited()
