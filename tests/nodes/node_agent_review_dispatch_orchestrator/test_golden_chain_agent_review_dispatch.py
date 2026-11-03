# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_agent_review_dispatch_orchestrator: contract, topics, entry point, one run."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_agent_review_dispatch_orchestrator import (
    NodeAgentReviewDispatchOrchestrator,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import load_topics
from tests.unit.nodes.node_agent_review_dispatch_orchestrator.support import (
    NODE,
    World,
    build_harness,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator/contract.yaml"
)


def test_the_contract_declares_one_command_and_three_terminal_topics() -> None:
    topics = load_topics()
    assert topics.command == "onex.cmd.omnimarket.agent-review-requested.v1"
    assert {topics.completed, topics.refused, topics.failed} == {
        "onex.evt.omnimarket.agent-review-completed.v1",
        "onex.evt.omnimarket.agent-review-refused.v1",
        "onex.evt.omnimarket.agent-review-failed.v1",
    }
    contract = yaml.safe_load(_CONTRACT.read_text("utf-8"))
    assert contract["name"] == NODE
    assert contract["event_bus"]["subscribe_topics"] == [topics.command]
    assert set(contract["event_bus"]["publish_topics"]) == {
        topics.completed,
        topics.refused,
        topics.failed,
    }


def test_the_node_is_registered_as_an_entry_point() -> None:
    names = {ep.name for ep in entry_points(group="onex.nodes")}
    assert NODE in names


async def test_the_node_class_runs_one_review_to_a_completed_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = build_harness("inmemory", monkeypatch, tmp_path, world=World())
    node = NodeAgentReviewDispatchOrchestrator(
        config=harness.config,
        head=harness.handler.head,
        lab=harness.handler.lab,
        signer=harness.handler.signer,
        store=harness.handler.store,
        poster=harness.handler.poster,
        terminal_log=harness.handler.terminal_log,
        clock=harness.handler.clock,
    )
    terminal = await node.handle(harness.request())
    assert terminal.outcome.value == "completed"


def test_the_node_class_constructs_as_the_runtime_builds_it() -> None:
    """No argument: the overlay and the ports are bound on the first request."""
    NodeAgentReviewDispatchOrchestrator()
