# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each repo reader of ``event_bus.subscribe_topics`` accepts this node's empty list and still sees its topics.

The contract declares ``event_bus.subscribe_topics: []`` so the shared runtime wires nothing; the command topic
lives in ``runtime_dispatch.command_topic``. A reader that required a non-empty list, or that provisioned or
graphed only ``subscribe_topics``, would drop the command topic or reject the contract. Each test below runs
one real reader on the real contract.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    load_topics,
)
from omnimarket.nodes.node_golden_chain_generator.handlers.handler_golden_chain_generator import (
    _extract_topics_from_contract,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.contract_facts import (
    read_contract_facts,
)
from omnimarket.seams.extraction import _extract_event_bus_edges
from omnimarket.validators.contract_topic_graph import parse_contract

pytestmark = pytest.mark.unit
NODE = "node_ci_checkout_effect"
COMMAND = "onex.cmd.omnimarket.ci-checkout-requested.v1"
TERMINALS = {
    "onex.evt.omnimarket.ci-checkout-completed.v1",
    "onex.evt.omnimarket.ci-checkout-failed.v1",
}


@pytest.fixture
def contract_path() -> Path:
    return Path(
        str(resources.files(f"omnimarket.nodes.{NODE}").joinpath("contract.yaml"))
    )


@pytest.fixture
def contract(contract_path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(contract_path.read_text())
    assert loaded["event_bus"]["subscribe_topics"] == []
    return loaded  # type: ignore[no-any-return]


def test_subscribe_topics_readers_contract_topics_helper_reads_an_empty_list(
    contract_path: Path, contract: dict[str, Any]
) -> None:
    assert contract_subscribe_topics(contract_path) == ()
    assert set(contract_publish_topics(contract_path)) == TERMINALS


def test_subscribe_topics_readers_the_serve_adapter_takes_the_command_topic_from_runtime_dispatch(
    contract: dict[str, Any],
) -> None:
    topics = load_topics()
    assert topics.command == COMMAND == contract["runtime_dispatch"]["command_topic"]
    assert {topics.success, topics.failure} == TERMINALS


def test_subscribe_topics_readers_topic_provisioning_still_provisions_the_command_topic(
    contract_path: Path, contract: dict[str, Any]
) -> None:
    facts = read_contract_facts([contract_path.parent])
    assert [f.contract_name for f in facts] == [NODE]
    assert COMMAND in facts[0].topics
    assert set(facts[0].topics) >= TERMINALS


def test_subscribe_topics_readers_the_topic_graph_still_sees_the_command_topic_as_consumed(
    contract_path: Path, contract: dict[str, Any]
) -> None:
    node = parse_contract(contract_path, "omnimarket")
    assert node is not None
    assert node.name == NODE
    # The graph reads runtime_dispatch.command_topic as a subscription, so the empty list opens no orphan.
    assert node.subscribe_topics == (COMMAND,)
    assert node.command_topic == COMMAND
    assert set(node.terminal_topics) >= TERMINALS
    # The command topic is declared as produced by an outside caller, so the graph has an owner for it.
    assert COMMAND in {topic for topic, _producer in node.externally_produced}


def test_subscribe_topics_readers_the_seam_extraction_emits_only_producer_edges(
    contract: dict[str, Any],
) -> None:
    edges = _extract_event_bus_edges(contract, "src/x/contract.yaml")
    assert {e.edge_id for e in edges} == TERMINALS
    assert all(e.role == "producer" for e in edges)


def test_subscribe_topics_readers_the_golden_chain_generator_reads_an_empty_list(
    contract: dict[str, Any],
) -> None:
    subscribe, publish = _extract_topics_from_contract(contract)
    assert subscribe == []
    assert set(publish) == TERMINALS
