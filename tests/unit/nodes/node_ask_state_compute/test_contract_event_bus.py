# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ask state node declares the bus mapping its command topic needs, so a demand can reach it (OMN-24993)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_ask_state_compute/contract.yaml"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_event_bus_subscribes_the_command_topic_and_publishes_both_terminal_events() -> (
    None
):
    contract = _contract()
    dispatch = contract["runtime_dispatch"]
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [dispatch["command_topic"]]
    assert bus["publish_topics"] == [
        dispatch["terminal_events"]["success"],
        dispatch["terminal_events"]["failure"],
    ]


def test_every_declared_topic_has_the_broker_grant_its_direction_needs() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    grants = contract["broker_acl_grants"]
    assert len(grants) == 1
    seen = {(g["name"], g["operation"]) for g in grants[0]["grants"]}
    for topic in bus["subscribe_topics"]:
        assert {(topic, "READ"), (topic, "DESCRIBE")} <= seen
    for topic in bus["publish_topics"]:
        assert {(topic, "WRITE"), (topic, "DESCRIBE")} <= seen
    assert len(seen) == len(grants[0]["grants"])


def test_result_events_are_declared_as_externally_consumed() -> None:
    contract = _contract()
    assert set(contract["externally_consumed_topics"]) == set(
        contract["event_bus"]["publish_topics"]
    )


def test_node_ask_state_compute_publishes_the_two_named_result_events() -> None:
    publish = _contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.ask-state-completed.v1" in publish
    assert "onex.evt.omnimarket.ask-state-failed.v1" in publish
