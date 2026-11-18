# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pins the topics node_dev_roll_window_gate_effect and node_redeploy_deploy_effect declare (state-coverage-gate)."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD,
)

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"

GATE_OUTPUT_TOPICS = [
    "onex.evt.omnimarket.dev-roll-window-held.v1",
    "onex.evt.omnimarket.dev-roll-window-refused.v1",
    "onex.evt.omnimarket.dev-roll-window-decision.v1",
    "onex.evt.omnimarket.dev-roll-window-recorded.v1",
]


def _publish_topics(node: str) -> list[str]:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    return list(contract["event_bus"]["publish_topics"])


def test_gate_publishes_each_declared_event_topic() -> None:
    published = _publish_topics("node_dev_roll_window_gate_effect")
    for topic in GATE_OUTPUT_TOPICS:
        assert topic in published


def test_deploy_effect_publishes_the_hold_topic_the_gate_subscribes() -> None:
    assert TOPIC_DEV_ROLL_HOLD == "onex.cmd.omnimarket.dev-roll-hold.v1"
    assert TOPIC_DEV_ROLL_HOLD in _publish_topics("node_redeploy_deploy_effect")
