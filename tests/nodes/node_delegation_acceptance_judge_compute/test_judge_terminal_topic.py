# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The acceptance judge's terminal event has its own topic, apart from the judged topic (OMN-26422).

The judge node returns a render, check, score or events result. The runtime publishes that result
on the contract's terminal event. When that topic was the judged-event topic, the eval-results
projection, which subscribes to it, was handed a render result and refused it as ``invalid_event``.
The judged topic carries only judged-acceptance events, which the publish effect node emits.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
JUDGE = "node_delegation_acceptance_judge_compute"
PUBLISH = "node_delegation_acceptance_judged_publish_effect"
PROJECTION = "node_projection_eval_results"
JUDGED_TOPIC = "onex.evt.omnimarket.delegation-acceptance-judged.v1"
JUDGE_COMPLETED_TOPIC = "onex.evt.omnimarket.delegation-acceptance-judge-completed.v1"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_judge_terminal_topic_is_not_the_judged_topic() -> None:
    assert _contract(JUDGE)["terminal_event"] != JUDGED_TOPIC


def test_judge_terminal_topic_is_its_own_declared_topic() -> None:
    contract = _contract(JUDGE)
    assert contract["terminal_event"] == JUDGE_COMPLETED_TOPIC
    assert contract["event_bus"]["publish_topics"] == [JUDGE_COMPLETED_TOPIC]
    assert contract["externally_consumed_topics"] == [JUDGE_COMPLETED_TOPIC]


def test_judge_terminal_topic_is_none_of_the_topics_the_judge_subscribes_to() -> None:
    contract = _contract(JUDGE)
    consumed = {
        *contract["event_bus"]["subscribe_topics"],
        contract["runtime_dispatch"]["command_topic"],
    }
    assert contract["terminal_event"] not in consumed


def test_judge_terminal_topic_runtime_route_publishes_off_the_judged_topic() -> None:
    route = _resolve_node_route(JUDGE)
    assert route.terminal_topic == JUDGE_COMPLETED_TOPIC


def test_judge_terminal_topic_judged_topic_is_not_declared_by_the_judge() -> None:
    """Only the publish effect, which emits the judged events, declares the judged topic."""
    contract = _contract(JUDGE)
    assert JUDGED_TOPIC not in contract["event_bus"]["publish_topics"]
    assert JUDGED_TOPIC not in contract.get("externally_consumed_topics", [])
    assert JUDGED_TOPIC in _contract(PUBLISH)["event_bus"]["publish_topics"]


def test_judge_terminal_topic_projection_reads_the_judged_topic_and_not_the_judge_result() -> (
    None
):
    projection = _contract(PROJECTION)
    subscribed = set(projection["event_bus"]["subscribe_topics"])
    assert JUDGED_TOPIC in subscribed
    assert JUDGE_COMPLETED_TOPIC not in subscribed
    assert _contract(JUDGE)["terminal_event"] not in subscribed
