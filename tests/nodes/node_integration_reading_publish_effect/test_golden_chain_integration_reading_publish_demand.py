# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration publication as a demand the runtime delivers (OMN-25036).

The contracts of the three integration-floor nodes gave the runtime nothing to deliver: the publish effect declared no
subscribe topic, so ``onex run-node`` refused it. This test reads the contract the way the runtime and the CLI do, runs the
runtime handler on a fake bus, and shows the envelope the floor's reader parses land on the snapshot topic. The control
drops the subscription and shows the same read refuse it.
"""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
)
from omnimarket.models.integration_reading import ModelIntegrationReadingBatch
from omnimarket.nodes.node_integration_reading_publish_effect.handlers import (
    HandlerIntegrationReadingPublishRequested,
)
from tests.nodes.node_integration_projection_compute.test_integration_projection import (
    BATCH,
    GOLDEN_COUNTS,
)

pytestmark = pytest.mark.unit

NODE = "node_integration_reading_publish_effect"
OVERLAY = ModelFloorProjectionPublishOverlay(
    bus="inmemory", projection_id="integration"
)


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return dict(yaml.safe_load(text))


def _declares_a_trigger(contract: dict[str, Any]) -> bool:
    """The runtime delivers a demand only where a subscribe topic, a routed handler and a terminal event are declared."""
    subscribed = contract["event_bus"]["subscribe_topics"]
    routed = {
        h["topic"] for h in contract["handler_routing"]["handlers"] if "topic" in h
    }
    return (
        bool(subscribed)
        and set(subscribed) <= routed
        and bool(contract.get("terminal_event"))
    )


class FakeBus:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []
        self._fail = fail

    async def publish(self, topic: str, *, key: bytes, value: bytes) -> None:
        if self._fail:
            raise ConnectionError("broker refused")
        self.sent.append((topic, key, value))


def _batch() -> ModelIntegrationReadingBatch:
    return ModelIntegrationReadingBatch.model_validate(BATCH)


def test_the_contract_declares_a_trigger_the_runtime_delivers() -> None:
    contract = _contract()
    assert _declares_a_trigger(contract)
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.integration-reading-published.v1"
    )


def test_named_break_without_the_subscription_the_trigger_read_refuses() -> None:
    contract = _contract()
    contract["event_bus"]["subscribe_topics"] = []
    assert not _declares_a_trigger(contract)


def test_the_demand_publishes_the_envelope_the_floor_reader_parses() -> None:
    contract = _contract()
    bus = FakeBus()
    handler = HandlerIntegrationReadingPublishRequested(bus, overlay=OVERLAY)
    result = asyncio.run(handler.handle(_batch()))
    assert result.published is True
    assert result.projection_id == "integration"
    (sent,) = bus.sent
    topic, key, value = sent
    assert topic == contract["runtime"]["topic"]
    assert key == b"integration"
    envelope = ModelFloorProjectionEnvelope.model_validate_json(value)
    assert envelope.projection_id == "integration"
    assert envelope.as_of == _batch().as_of
    assert envelope.producer.startswith(f"{NODE}:")
    counts = json.loads(value)["payload"]["counts"]
    for name, count in GOLDEN_COUNTS.items():
        assert counts[name] == count


def test_a_refusing_bus_is_a_typed_result_never_a_raise() -> None:
    handler = HandlerIntegrationReadingPublishRequested(
        FakeBus(fail=True), overlay=OVERLAY
    )
    result = asyncio.run(handler.handle(_batch()))
    assert result.published is False
    assert result.reason == "integration_publish_failed: ConnectionError"


def test_an_overlay_that_cannot_be_read_publishes_nothing() -> None:
    bus = FakeBus()
    handler = HandlerIntegrationReadingPublishRequested(
        bus, environ={"ONEX_FLOOR_INTEGRATION_OVERLAY": "/nonexistent/overlay.yaml"}
    )
    result = asyncio.run(handler.handle(_batch()))
    assert result.published is False
    assert result.reason.startswith("integration_overlay_unreadable")
    assert bus.sent == []
