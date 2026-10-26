# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged contract of node_host_pool_alarm_effect and its onex.nodes registration."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_pool_alarm_effect"


def load_contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def test_the_contract_names_its_handler_command_topic_and_terminal_event() -> None:
    contract = load_contract()
    assert contract["name"] == "node_host_pool_alarm_effect"
    assert contract["handler"]["class"] == "HandlerHostPoolAlarm"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]), "HandlerHostPoolAlarm"
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.host-pool-alarm-requested.v1"
    )
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.host-pool-alarm-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.host-pool-alarm-completed.v1"
    ]
    assert contract["externally_consumed_topics"] == [
        "onex.evt.omnimarket.host-pool-alarm-completed.v1"
    ]
    assert (
        "onex.cmd.omnimarket.host-pool-alarm-requested.v1"
        in contract["event_bus"]["subscribe_topics"]
    )


def test_the_node_is_a_registered_entry_point_with_matching_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        entry_points["node_host_pool_alarm_effect"]
        == "omnimarket.nodes.node_host_pool_alarm_effect"
    )
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "node_host_pool_alarm_effect"
    assert (
        metadata["entry_points"]["onex.nodes"]["node_host_pool_alarm_effect"]
        == "omnimarket.nodes.node_host_pool_alarm_effect"
    )
    assert (
        importlib.import_module("omnimarket.nodes.node_host_pool_alarm_effect").__name__
        == "omnimarket.nodes.node_host_pool_alarm_effect"
    )
