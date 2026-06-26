# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived schema tests for node_regression_test_orchestrator (OMN-13616).

These assert the ORCHESTRATOR archetype constraints the ticket DoD mandates:
contract-declared, handler routed, topics contract-sourced, terminal in
publish_topics, archetype == orchestrator.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_regression_test_orchestrator"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"

SUBSCRIBE_TOPIC = "onex.cmd.omnimarket.regression-suite-start.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.regression-suite-completed.v1"


@pytest.mark.unit
def test_contract_yaml_is_well_formed() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["name"] == "regression_test_orchestrator"
    assert data["node_type"] == "orchestrator"
    assert data["contract_version"]["major"] == 1


@pytest.mark.unit
def test_contract_is_orchestrator_archetype() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["descriptor"]["node_archetype"] == "orchestrator"


@pytest.mark.unit
def test_contract_declares_expected_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    bus = data["event_bus"]
    assert SUBSCRIBE_TOPIC in bus["subscribe_topics"]
    assert TERMINAL_TOPIC in bus["publish_topics"]


@pytest.mark.unit
def test_contract_terminal_event_matches_publish_topic() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["terminal_event"] == TERMINAL_TOPIC
    assert data["terminal_event"] in data["event_bus"]["publish_topics"]


@pytest.mark.unit
def test_contract_handler_routing_points_at_real_handler() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    entry = data["handler_routing"]["handlers"][0]
    assert entry["handler"]["name"] == "HandlerRegressionTestOrchestrator"
    assert entry["handler"]["module"].startswith(
        "omnimarket.nodes.node_regression_test_orchestrator"
    )
    assert entry["event_model"]["name"] == "ModelRegressionSuiteStart"
