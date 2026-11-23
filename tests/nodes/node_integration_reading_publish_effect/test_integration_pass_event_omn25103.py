# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for OMN-25103 integration pass event publishing."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_integration_reading_publish_effect import (
    runtime_integration_reading_publish as runtime,
)
from tests.nodes.node_integration_reading_publish_effect.test_integration_reading_publish import (
    EXAMPLE,
    NODE,
    OVERLAY,
    _batch,
    _contract,
)

EVENT_TOPIC: str = "onex.evt.omnimarket.floor-integration-pass.v1"


def test_publisher_emits_integration_pass_event() -> None:
    contract: Any = _contract(NODE)
    assert EVENT_TOPIC in contract["event_bus"]["publish_topics"]
    assert contract["runtime"]["event_topic"] == EVENT_TOPIC

    snapshot = InMemoryEnvelopePublisher()
    event = InMemoryEnvelopePublisher()

    asyncio.run(
        runtime.run(
            _batch(),
            OVERLAY,
            snapshot,
            InMemoryEmissionSink(),
            producer="pass",
            event_publisher=event,
        )
    )

    assert len(snapshot.published) == 1
    assert len(event.published) == 1
    assert snapshot.published[0].payload == event.published[0].payload
    assert snapshot.published[0].as_of == event.published[0].as_of


def test_publisher_cli_publishes_the_pass_on_both_topics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded_topics: list[str] = []
    publishers: list[InMemoryEnvelopePublisher] = []

    def build_publisher(
        overlay: Any, env: Any, topic: str
    ) -> InMemoryEnvelopePublisher:
        recorded_topics.append(topic)
        publisher = InMemoryEnvelopePublisher()
        publishers.append(publisher)
        return publisher

    monkeypatch.setattr(runtime, "build_publisher", build_publisher)
    monkeypatch.setenv("ONEX_FLOOR_INTEGRATION_OVERLAY", str(tmp_path / "overlay.yaml"))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-roots"))
    (tmp_path / "overlay.yaml").write_text(EXAMPLE, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(_batch().model_dump_json()))

    result = runtime.main(["--home", str(tmp_path)])
    assert result == 0

    assert set(recorded_topics) == {_contract(NODE)["runtime"]["topic"], EVENT_TOPIC}
    assert len(publishers) == 2
    for publisher in publishers:
        assert len(publisher.published) == 1
    assert publishers[0].published[0].payload == publishers[1].published[0].payload


def test_publisher_without_event_publisher_publishes_only_the_snapshot() -> None:
    snapshot = InMemoryEnvelopePublisher()

    asyncio.run(
        runtime.run(
            _batch(),
            OVERLAY,
            snapshot,
            InMemoryEmissionSink(),
            producer="pass",
        )
    )

    assert len(snapshot.published) == 1
