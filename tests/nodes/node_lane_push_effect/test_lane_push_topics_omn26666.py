# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_push_effect declares its result and failure topics (OMN-26666)."""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_push_effect/contract.yaml"
)


def test_completed_and_failed_topics_are_published() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    publish = contract["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.lane-push-completed.v1" in publish
    assert "onex.evt.omnimarket.lane-push-failed.v1" in publish
    assert contract["terminal_event"] == "onex.evt.omnimarket.lane-push-completed.v1"
    assert (
        contract["runtime_dispatch"]["terminal_events"]["failure"]
        == "onex.evt.omnimarket.lane-push-failed.v1"
    )
