# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the topics the scheduled work-state projection declares, against the nodes that share them (OMN-22935).

Fails when this node stops subscribing the runtime tick that node_floor_simple_scheduled_effect also subscribes, when its
terminal event is not published, or when a second node publishes the same terminal topic.
"""

from __future__ import annotations

import pytest
import yaml

from tests.nodes.node_work_state_projection_scheduled_effect.support import SRC

pytestmark = pytest.mark.unit

NODE = "node_work_state_projection_scheduled_effect"
TERMINAL = "onex.evt.omnimarket.work-state-projected.v1"
TICK = "onex.intent.platform.runtime-tick.v1"


def _contract(node: str) -> dict:
    return yaml.safe_load((SRC / node / "contract.yaml").read_text(encoding="utf-8"))


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = _contract(NODE)
    assert loaded["terminal_event"] == TERMINAL
    assert TERMINAL in loaded["event_bus"]["publish_topics"]
    assert loaded["published_events"][0]["topic"] == TERMINAL


def test_the_tick_topic_is_the_one_the_scheduled_floor_subscribes() -> None:
    mine = _contract(NODE)
    floor = _contract("node_floor_simple_scheduled_effect")
    assert mine["input_subscriptions"][0]["topic"] == TICK
    assert TICK in mine["event_bus"]["subscribe_topics"]
    assert TICK in floor["event_bus"]["subscribe_topics"]


def test_no_other_node_publishes_the_terminal_topic() -> None:
    publishers = sorted(
        path.parent.name
        for path in SRC.glob("*/contract.yaml")
        if TERMINAL
        in (
            (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("event_bus")
            or {}
        ).get("publish_topics", ())
    )
    assert publishers == [NODE]


def test_the_output_model_the_contract_names_is_the_event_the_handler_returns() -> None:
    from omnimarket.models.work_state_projection import ModelWorkStateProjectedEvent

    out = _contract(NODE)["output_model"]
    assert out["name"] == ModelWorkStateProjectedEvent.__name__
    assert out["module"] == ModelWorkStateProjectedEvent.__module__
