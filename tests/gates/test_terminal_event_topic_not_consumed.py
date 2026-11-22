# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No node publishes its terminal event onto a topic it consumes (OMN-26422).

A node's terminal event carries the node's own result shape. When that topic is also one the node
consumes, or one a projection subscribes to for a different shape, every consumer of the topic is
handed a payload it refuses. The delegation acceptance judge did exactly that: its render result
landed on the judged topic that the eval-results projection reads, and the projection refused it
as ``invalid_event`` (68 of 69 zero-row dispatches in one dev window).

The check reads every ``src/omnimarket/nodes/*/contract.yaml``. A topic a node consumes is anything
in ``event_bus.subscribe_topics``, its ``runtime_dispatch.command_topic`` or the ``topic`` of a
``handler_routing`` entry. ``TERMINAL_EVENT_CONSUMED_DEBT`` freezes any node that already violates
the rule; it may only shrink.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"

JUDGE_NODE = "node_delegation_acceptance_judge_compute"
PROJECTION_NODE = "node_projection_eval_results"

# Nodes that publish their terminal event onto a topic they consume. Burn-down list: remove a
# node when its contract is fixed; adding one is a failed review, not a way to pass this test.
# node_code_eval_trial_orchestrator subscribes to its own code-eval-run-closed event.
TERMINAL_EVENT_CONSUMED_DEBT: frozenset[str] = frozenset(
    {"node_code_eval_trial_orchestrator"}
)


def _contracts() -> dict[str, dict[str, Any]]:
    return {
        path.parent.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(NODES.glob("node_*/contract.yaml"))
    }


def consumed_topics(contract: dict[str, Any]) -> set[str]:
    """Every topic the contract names as one the node reads."""
    topics: set[str] = set()
    bus = contract.get("event_bus") or {}
    topics.update(bus.get("subscribe_topics") or [])
    dispatch = contract.get("runtime_dispatch") or {}
    command_topic = dispatch.get("command_topic")
    if isinstance(command_topic, str):
        topics.add(command_topic)
    routing = contract.get("handler_routing") or {}
    for entry in routing.get("handlers") or []:
        topic = entry.get("topic") if isinstance(entry, dict) else None
        if isinstance(topic, str):
            topics.add(topic)
    return topics


def terminal_event_consumed_violators(
    contracts: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Node name to its terminal event, for each node that consumes that very topic."""
    violators: dict[str, str] = {}
    for name, contract in contracts.items():
        terminal = contract.get("terminal_event")
        if isinstance(terminal, str) and terminal in consumed_topics(contract):
            violators[name] = terminal
    return violators


def test_terminal_event_topic_not_consumed_by_its_own_node() -> None:
    violators = terminal_event_consumed_violators(_contracts())
    new = {n: t for n, t in violators.items() if n not in TERMINAL_EVENT_CONSUMED_DEBT}
    assert not new, (
        f"these nodes publish their terminal event onto a topic they consume: {new}. "
        "Give the terminal event its own topic."
    )


def test_terminal_event_topic_not_consumed_debt_only_shrinks() -> None:
    stale = sorted(
        TERMINAL_EVENT_CONSUMED_DEBT
        - set(terminal_event_consumed_violators(_contracts()))
    )
    assert not stale, f"fixed nodes still frozen in the debt list: {stale}"


def test_terminal_event_topic_not_consumed_detects_a_self_consuming_node() -> None:
    """Positive control: the check names a node whose terminal event is also a subscription."""
    contracts = {
        "node_bad": {
            "terminal_event": "onex.evt.x.done.v1",
            "event_bus": {"subscribe_topics": ["onex.evt.x.done.v1"]},
        },
        "node_good": {
            "terminal_event": "onex.evt.x.done.v1",
            "event_bus": {"subscribe_topics": ["onex.cmd.x.requested.v1"]},
            "runtime_dispatch": {"command_topic": "onex.cmd.x.requested.v1"},
        },
        "node_routed": {
            "terminal_event": "onex.evt.y.done.v1",
            "handler_routing": {"handlers": [{"topic": "onex.evt.y.done.v1"}]},
        },
    }
    assert terminal_event_consumed_violators(contracts) == {
        "node_bad": "onex.evt.x.done.v1",
        "node_routed": "onex.evt.y.done.v1",
    }


def test_terminal_event_topic_not_consumed_judge_result_never_on_projection_topic() -> (
    None
):
    """The judge's result shape is never published on a topic the eval-results projection reads."""
    contracts = _contracts()
    judge = contracts[JUDGE_NODE]
    projection_topics = consumed_topics(contracts[PROJECTION_NODE])
    judge_published = {judge["terminal_event"], *judge["event_bus"]["publish_topics"]}
    assert not judge_published & projection_topics, (
        f"the judge publishes {sorted(judge_published & projection_topics)} which the "
        "eval-results projection subscribes to"
    )
