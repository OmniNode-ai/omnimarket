# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's reader takes the integration snapshot as the runtime delivers it (OMN-25036).

The snapshot topics carry no message category in their name, so the runtime cannot dispatch them and the reader
subscribes to none; the contract declares the handler's operation and the topic the pass reads. This handler reads one
publication the way the pass does: the publication names the projection the overlay names for that topic, and it keeps
its producer's own time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import resources
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
    ModelFloorProjectionSpec,
)
from omnimarket.nodes.node_floor_projection_read_effect.handlers import (
    HandlerFloorProjectionSnapshotRead,
)

pytestmark = pytest.mark.unit

NODE = "node_floor_projection_read_effect"
AS_OF = datetime(2026, 11, 17, 19, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 17, 19, 5, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return dict(yaml.safe_load(text))


TOPIC = "onex.snapshot.projection.floor-integration.v1"  # onex-topic-allow: a projection snapshot topic has the projection's own name form.
OVERLAY = ModelFloorProjectionReadOverlay(
    bus="inmemory",
    read_window_s=1.0,
    projections={
        "integration": ModelFloorProjectionSpec(topic=TOPIC, max_age_s=7800),
        "owner_map": ModelFloorProjectionSpec(topic="x.other.v1", max_age_s=900),
    },
)


def _handler() -> HandlerFloorProjectionSnapshotRead:
    return HandlerFloorProjectionSnapshotRead(overlay=OVERLAY, clock=lambda: NOW)


def _envelope(projection_id: str) -> ModelFloorProjectionEnvelope:
    return ModelFloorProjectionEnvelope(
        projection_id=projection_id,
        as_of=AS_OF,
        producer="node_integration_reading_publish_effect:h201",
        payload={"counts": {"merged": 3}},
    )


def test_the_contract_reads_the_integration_snapshot_topic_without_a_runtime_subscription() -> (
    None
):
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == []
    assert TOPIC in contract["snapshot_reads"]
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] == "onex.evt.omnimarket.floor-projection-read.v1"


def test_a_delivered_publication_reads_with_its_producers_time() -> None:
    entry = _handler().handle(_envelope("integration"))
    assert entry.status == "read"
    assert entry.projection_id == "integration"
    assert entry.as_of == AS_OF
    assert entry.max_age_s == 7800
    assert entry.read_at == NOW
    assert entry.payload == {"counts": {"merged": 3}}


def test_a_publication_of_another_projection_is_unread_never_data() -> None:
    entry = _handler().handle(_envelope("owner_map"))
    assert entry.status == "unread"
    assert entry.payload == {}
    assert "owner_map" in (entry.reason or "")
