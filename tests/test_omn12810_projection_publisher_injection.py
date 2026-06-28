# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12810 — projection handlers must not own AIOKafkaProducer.

Contract: the projection *runtime* (``BaseProjectionRunner``) owns the Kafka
producer lifecycle and injects the publish callable. The delegation projection
handler must not construct ``AIOKafkaProducer`` itself, and the producer-building
``_get_publish_fn`` method must be gone from the handler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers import handler_delegation
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_savings.handlers import handler_savings
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)

HANDLER_SOURCE = Path(handler_delegation.__file__).read_text(encoding="utf-8")
SAVINGS_SOURCE = Path(handler_savings.__file__).read_text(encoding="utf-8")


@pytest.mark.unit
def test_handler_module_does_not_reference_aiokafkaproducer() -> None:
    """DoD: the projection node module must not name AIOKafkaProducer at all."""
    assert "AIOKafkaProducer" not in HANDLER_SOURCE


@pytest.mark.unit
def test_handler_has_no_private_producer_builder() -> None:
    """The producer-building _get_publish_fn must be deleted from the handler."""
    assert not hasattr(DelegationProjectionRunner, "_get_publish_fn")
    assert "_get_publish_fn" not in HANDLER_SOURCE
    # The handler must not hold its own producer field anymore.
    runner = DelegationProjectionRunner()
    assert not hasattr(runner, "_producer") or runner.__dict__.get("_producer") is None


@pytest.mark.unit
def test_sibling_savings_handler_also_delegates_to_runtime() -> None:
    """The savings projection handler shares the no-owned-producer contract."""
    assert "AIOKafkaProducer" not in SAVINGS_SOURCE
    assert "_get_publish_fn" not in SAVINGS_SOURCE


@pytest.mark.unit
def test_runtime_base_owns_publisher_lifecycle() -> None:
    """The runtime base class exposes the publisher accessor handlers delegate to."""
    assert hasattr(BaseProjectionRunner, "get_publish_fn")


@pytest.mark.unit
def test_injected_publish_fn_flows_through_terminal_emit() -> None:
    """An injected publish_fn is used for terminal-event emission (no producer build)."""
    published: list[tuple[str, bytes]] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    runner = DelegationProjectionRunner(publish_fn=capture)
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=None)
    runner._db = mock_db

    topic = runner.subscribe_topics[0]
    data: dict[str, Any] = {
        "correlation_id": "omn12810-c1",
        "task_type": "code-review",
        "delegated_to": "agent-alpha",
    }
    meta = MessageMeta(partition=0, offset=0, fallback_id="omn12810-c1")
    asyncio.run(runner.project_event(topic, data, meta))

    assert len(published) == 1
    assert published[0][0] == runner._terminal_topic


@pytest.mark.unit
def test_runtime_builds_producer_lazily_when_no_injection() -> None:
    """When no publish_fn is injected, the runtime base builds and owns the producer."""

    class _StubRunner(BaseProjectionRunner):
        @property
        def topics(self) -> list[str]:
            return []

        async def project_event(
            self, topic: str, data: dict[str, Any], meta: MessageMeta
        ) -> bool:
            return True

    binding = ModelProjectionRuntimeBinding(
        kafka_bootstrap_servers="localhost:9092",
        database_url="postgresql://stub/stub",  # type: ignore[arg-type]
    )
    runner = _StubRunner(runtime_binding=binding)

    fake_producer = MagicMock()
    fake_producer.start = AsyncMock()
    fake_producer.stop = AsyncMock()
    fake_producer.send_and_wait = AsyncMock()

    with patch(
        "omnimarket.projection.runner.AIOKafkaProducer",
        return_value=fake_producer,
    ) as ctor:
        publish = asyncio.run(runner.get_publish_fn())
        assert publish is not None
        ctor.assert_called_once()
        fake_producer.start.assert_awaited_once()

        asyncio.run(publish("some-topic", b"payload"))
        fake_producer.send_and_wait.assert_awaited_once_with("some-topic", b"payload")

        # Second call must reuse the same producer (lifecycle owned once).
        asyncio.run(runner.get_publish_fn())
        ctor.assert_called_once()
