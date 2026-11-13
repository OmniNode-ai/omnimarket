"""The node's declared topics each resolve to a handler_routing entry (node_lane_gh_read_guard_effect)."""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_gh_read_guard_effect/contract.yaml"
)
READ_CMD = "onex.cmd.omniclaude.lane-github-read-check.v1"
SNAP_CMD = "onex.cmd.omniclaude.runner-placement-snapshot-check.v1"
READ_EVT = "onex.evt.omniclaude.lane-github-read-checked.v1"
SNAP_EVT = "onex.evt.omniclaude.runner-placement-snapshot-checked.v1"


def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def test_each_subscribed_command_topic_has_a_command_route() -> None:
    contract = _contract()
    routes = {h["topic"]: h for h in contract["handler_routing"]["handlers"]}
    assert contract["handler_routing"]["routing_strategy"] == "topic_match"
    assert set(contract["event_bus"]["subscribe_topics"]) == {READ_CMD, SNAP_CMD}
    for topic in (READ_CMD, SNAP_CMD):
        assert routes[topic]["message_category"] == "command"


def test_published_event_topics_are_declared() -> None:
    contract = _contract()
    assert contract["event_bus"]["publish_topics"] == [READ_EVT, SNAP_EVT]
    assert contract["terminal_event"] == READ_EVT
