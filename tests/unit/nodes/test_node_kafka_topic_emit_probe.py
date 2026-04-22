"""
Unit tests for node_kafka_topic_emit_probe.
"""

import pytest

from omnimarket.nodes.node_kafka_topic_emit_probe.handlers.handler_kafka_probe import (
    HandlerKafkaProbe,
)
from omnimarket.nodes.node_kafka_topic_emit_probe.models.model_kafka_probe_request import (
    ModelKafkaProbeRequest,
)


@pytest.mark.asyncio
async def test_handler_initialization():
    """Handler should initialize without error."""
    handler = HandlerKafkaProbe()
    await handler.initialize()
    assert handler._initialized is True


@pytest.mark.asyncio
async def test_handle_empty_topics():
    """Handler should work with empty topics list (uses declared defaults)."""
    handler = HandlerKafkaProbe()
    await handler.initialize()

    data = ModelKafkaProbeRequest(
        topics=[],
        probe_interval_seconds=0,
        verify_consumers=False,
    )
    result = await handler.handle(data)

    assert "probes_emitted" in result
    assert "consumers_advanced" in result
    assert "failures" in result
    assert isinstance(result["probes_emitted"], int)
    assert isinstance(result["consumers_advanced"], int)
    assert isinstance(result["failures"], list)


@pytest.mark.asyncio
async def test_handle_specific_topics():
    """Handler should probe specific topics when provided."""
    handler = HandlerKafkaProbe()
    await handler.initialize()

    topics = ["topic_a", "topic_b"]
    data = ModelKafkaProbeRequest(
        topics=topics, probe_interval_seconds=0, verify_consumers=False
    )
    result = await handler.handle(data)

    assert result["probes_emitted"] == len(topics)
    assert result["consumers_advanced"] == len(topics)
    assert result["failures"] == []
