# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic wiring tests for node_overnight."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    TOPIC_OVERNIGHT_COMPLETE,
    TOPIC_OVERNIGHT_FAILED,
    TOPIC_OVERNIGHT_PHASE_END,
    TOPIC_OVERNIGHT_PHASE_START,
    TOPIC_OVERNIGHT_START,
    HandlerOvernight,
    ModelOvernightCommand,
    _load_topic_bindings,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT = (
    _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_overnight" / "contract.yaml"
)
_HANDLER = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_overnight"
    / "handlers"
    / "handler_overnight.py"
)


class _RecordingBus:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, json.loads(payload.decode())))


def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _single(topics: list[str], fragment: str) -> str:
    matches = [topic for topic in topics if fragment in topic]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.unit
def test_topic_constants_match_contract_event_bus_topics() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    subscribe_topics = event_bus["subscribe_topics"]
    publish_topics = event_bus["publish_topics"]
    assert isinstance(subscribe_topics, list)
    assert isinstance(publish_topics, list)

    assert _single(subscribe_topics, "overnight-start") == TOPIC_OVERNIGHT_START
    assert _single(publish_topics, "overnight-start") == TOPIC_OVERNIGHT_START
    assert (
        _single(publish_topics, "overnight-session-completed")
        == TOPIC_OVERNIGHT_COMPLETE
    )
    assert _single(publish_topics, "overnight-session-failed") == TOPIC_OVERNIGHT_FAILED
    assert (
        _single(publish_topics, "overnight-phase-completed")
        == TOPIC_OVERNIGHT_PHASE_END
    )
    assert (
        _single(publish_topics, "overnight-phase-start") == TOPIC_OVERNIGHT_PHASE_START
    )


@pytest.mark.unit
def test_injected_contract_path_controls_runtime_topic_bindings(tmp_path: Path) -> None:
    raw = _contract()
    raw["event_bus"] = {
        "subscribe_topics": [
            "custom.cmd.omnimarket.overnight-start.v2",
        ],
        "publish_topics": [
            "custom.evt.omnimarket.overnight-phase-start.v2",
            "custom.evt.omnimarket.overnight-phase-completed.v2",
            "custom.evt.omnimarket.overnight-session-completed.v2",
            "custom.cmd.omnimarket.overnight-start.v2",
            "custom.evt.omnimarket.overnight-session-failed.v2",
        ],
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    topics = _load_topic_bindings(contract_path)
    assert topics.start == "custom.cmd.omnimarket.overnight-start.v2"
    assert topics.phase_start == "custom.evt.omnimarket.overnight-phase-start.v2"
    assert topics.phase_end == "custom.evt.omnimarket.overnight-phase-completed.v2"
    assert topics.complete == "custom.evt.omnimarket.overnight-session-completed.v2"
    assert topics.failed == "custom.evt.omnimarket.overnight-session-failed.v2"

    bus = _RecordingBus()
    handler = HandlerOvernight(event_bus=bus, contract_path=contract_path)
    handler.handle(ModelOvernightCommand(correlation_id="custom-topics", dry_run=True))

    published_topics = [topic for topic, _ in bus.calls]
    assert published_topics.count(topics.phase_start) == 5
    assert published_topics.count(topics.phase_end) == 5
    assert published_topics[-2:] == [topics.complete, topics.start]


@pytest.mark.unit
def test_handler_source_does_not_own_onex_topic_literals() -> None:
    source = _HANDLER.read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
    assert "onex-topic-allow" not in source
