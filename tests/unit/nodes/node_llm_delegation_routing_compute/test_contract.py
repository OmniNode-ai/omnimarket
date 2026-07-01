# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract tests for node_llm_delegation_routing_compute."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_llm_delegation_routing_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_contract_declares_runtime_dispatch_topic_literal_values() -> None:
    """Lock the declared runtime_dispatch topics to their literal wire strings.

    A regression test, not a gate-satisfaction placeholder: this pure COMPUTE
    node has no handler-owned publish logic (the runtime auto-publishes the
    contract's ``runtime_dispatch.terminal_events`` based on whether
    ``HandlerDelegationRouting.handle()`` returns normally or raises); pinning
    the literal topic strings here catches a silent contract rename that
    would otherwise only surface at a live runtime boundary.
    """
    contract = _load_contract()
    rd = contract["runtime_dispatch"]
    assert rd["command_topic"] == "onex.cmd.omnimarket.llm-delegation-routing.v1"
    assert (
        rd["terminal_events"]["success"]
        == "onex.evt.omnimarket.llm-delegation-routing-completed.v1"
    )
    assert (
        rd["terminal_events"]["failure"]
        == "onex.evt.omnimarket.llm-delegation-routing-failed.v1"
    )


def test_contract_event_bus_topics_match_runtime_dispatch() -> None:
    contract = _load_contract()
    rd = contract["runtime_dispatch"]
    eb = contract["event_bus"]
    assert rd["command_topic"] in eb["subscribe_topics"]
    assert rd["terminal_events"]["success"] in eb["publish_topics"]
    assert rd["terminal_events"]["failure"] in eb["publish_topics"]
