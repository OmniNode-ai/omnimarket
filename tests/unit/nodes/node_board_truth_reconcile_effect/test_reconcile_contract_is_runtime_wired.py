# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconcile effect is reachable on a deployed runtime (OMN-16731).

The pre-merge lab proof of omnimarket#2872 booted runtime-effects with this
node installed and it logged ``skipping Kafka subscription for contract
node_board_truth_reconcile_effect ... reason=No event_bus.subscribe_topics
declared in contract``. The contract listed its topics at the top level, which
the runtime's auto-wiring never reads, so the command topic in the contract's
own golden path reached nothing.

These tests parse the contract with the runtime's own discovery code and hold
it to the same condition the runtime applies before it subscribes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring import ModelDiscoveredContract
from omnibase_infra.runtime.auto_wiring.discovery import (
    discover_contracts_from_paths,
)

import omnimarket.nodes.node_board_truth_reconcile_effect as node_pkg

pytestmark = pytest.mark.unit

_CONTRACT = Path(node_pkg.__file__).parent / "contract.yaml"
_COMMAND_TOPIC = "onex.cmd.omnimarket.board-truth-reconcile.v1"
_TERMINAL_TOPIC = "onex.evt.omnimarket.board-truth-reconciled.v1"


def _discovered() -> ModelDiscoveredContract:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 1
    return manifest.contracts[0]


def test_the_runtime_sees_a_subscribe_topic_for_the_reconcile_command() -> None:
    contract = _discovered()
    event_bus = contract.event_bus
    assert event_bus is not None, (
        "no event_bus block: the runtime skips the Kafka subscription"
    )
    assert _COMMAND_TOPIC in event_bus.subscribe_topics


def test_the_runtime_sees_the_terminal_topic_as_a_publish_topic() -> None:
    contract = _discovered()
    event_bus = contract.event_bus
    assert event_bus is not None
    assert _TERMINAL_TOPIC in event_bus.publish_topics


def test_the_contract_names_the_handler_and_its_input_model() -> None:
    raw = yaml.safe_load(_CONTRACT.read_text())
    assert raw["handler"]["class"] == "HandlerBoardTruthReconcile"
    assert raw["handler"]["module"].endswith("handlers.handler_board_truth_reconcile")
    assert raw["handler"]["input_model"].endswith(
        "model_board_reconcile_input.ModelBoardReconcileInput"
    )
    assert raw["terminal_event"] == _TERMINAL_TOPIC


def test_topics_are_declared_once_under_event_bus_not_at_the_top_level() -> None:
    raw = yaml.safe_load(_CONTRACT.read_text())
    assert "subscribe_topics" not in raw
    assert "publish_topics" not in raw
