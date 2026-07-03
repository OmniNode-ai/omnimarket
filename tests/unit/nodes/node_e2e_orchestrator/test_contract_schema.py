# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract schema tests for node_e2e_orchestrator (OMN-13692).

Verifies that the contract uses the canonical event_bus schema
(event_bus.subscribe_topics / event_bus.publish_topics) rather than the
legacy topics.subscribes / topics.publishes layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_e2e_orchestrator.consumer import (
    TOPIC_BUILD_COMPLETED,
    TOPIC_PR_LIFECYCLE_COMPLETED,
    TOPIC_PR_LIFECYCLE_START,
)

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_e2e_orchestrator"
    / "contract.yaml"
)


def _load_contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "contract.yaml must be a YAML mapping"
    return raw


@pytest.mark.unit
def test_contract_has_event_bus_block_not_legacy_topics() -> None:
    """contract.yaml must use event_bus, not the legacy topics block."""
    contract = _load_contract()
    assert "event_bus" in contract, (
        "contract.yaml missing canonical 'event_bus' block — "
        "rename topics.subscribes/publishes to event_bus.subscribe_topics/publish_topics"
    )
    assert "topics" not in contract, (
        "contract.yaml still has legacy 'topics' block — remove it in favour of 'event_bus'"
    )


@pytest.mark.unit
def test_event_bus_subscribe_topics_present() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    subscribe_topics = event_bus.get("subscribe_topics")
    assert isinstance(subscribe_topics, list), (
        "event_bus.subscribe_topics must be a list"
    )
    assert len(subscribe_topics) > 0, "event_bus.subscribe_topics must be non-empty"


@pytest.mark.unit
def test_event_bus_publish_topics_present() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    publish_topics = event_bus.get("publish_topics")
    assert isinstance(publish_topics, list), "event_bus.publish_topics must be a list"
    assert len(publish_topics) > 0, "event_bus.publish_topics must be non-empty"


@pytest.mark.unit
def test_subscribe_topics_match_handler_constants() -> None:
    """Topics in event_bus.subscribe_topics must match the constants used in consumer.py."""
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    subscribe_topics: list[str] = list(event_bus["subscribe_topics"])

    assert TOPIC_BUILD_COMPLETED in subscribe_topics, (
        f"{TOPIC_BUILD_COMPLETED!r} missing from event_bus.subscribe_topics"
    )
    assert TOPIC_PR_LIFECYCLE_COMPLETED in subscribe_topics, (
        f"{TOPIC_PR_LIFECYCLE_COMPLETED!r} missing from event_bus.subscribe_topics"
    )


@pytest.mark.unit
def test_publish_topics_match_handler_constants() -> None:
    """Topics in event_bus.publish_topics must match the constants used in consumer.py."""
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    publish_topics: list[str] = list(event_bus["publish_topics"])

    assert TOPIC_PR_LIFECYCLE_START in publish_topics, (
        f"{TOPIC_PR_LIFECYCLE_START!r} missing from event_bus.publish_topics"
    )


@pytest.mark.unit
def test_runtime_dispatch_event_consumer_exemption_present() -> None:
    """The event_consumer invocation_mode exemption must be retained."""
    contract = _load_contract()
    runtime_dispatch = contract.get("runtime_dispatch")
    assert isinstance(runtime_dispatch, dict), "runtime_dispatch block missing"
    assert runtime_dispatch.get("invocation_mode") == "event_consumer", (
        "runtime_dispatch.invocation_mode must be 'event_consumer'"
    )
    assert runtime_dispatch.get("reason"), (
        "runtime_dispatch.reason rationale must be non-empty"
    )
