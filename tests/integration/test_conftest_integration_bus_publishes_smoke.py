"""Smoke test: integration harness bootstrap verification.

Verifies that:
1. The @pytest.mark.integration marker is discoverable.
2. The integration_event_bus fixture yields a working EventBusInmemory.
3. Events published to the bus are recorded in event history.
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory


@pytest.mark.integration
async def test_conftest_integration_bus_publishes_smoke(
    integration_event_bus: EventBusInmemory,
) -> None:
    """Bus fixture yields a live EventBusInmemory that records published events."""
    topic = "onex.evt.omnimarket.integration-smoke.v1"
    await integration_event_bus.start()
    try:
        await integration_event_bus.publish(
            topic=topic,
            key=None,
            value=json.dumps({"status": "smoke"}).encode(),
        )
        history = await integration_event_bus.get_event_history(topic=topic)
        assert len(history) >= 1, "Expected at least one published event in history"
        assert any(e.topic == topic for e in history)
    finally:
        await integration_event_bus.close()
