# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_conservation_compute: the contract declares the reconcile and feed routes and the terminal."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_seam_conservation_compute as node_package

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
RECONCILE_TOPIC = "onex.cmd.omnimarket.view-seam-reconcile-requested.v1"
GRAPH_TOPIC = "onex.cmd.omnimarket.view-seam-graph-check-requested.v1"
FEED_TOPIC = "onex.cmd.omnimarket.view-seam-readings-feed-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.view-seam-reading-recorded.v1"
COUNT_TOPIC = "onex.evt.omnimarket.view-seam-counted.v1"
CLOSE_TOPIC = "onex.evt.omnimarket.view-seam-window-closed.v1"


def _contract() -> dict[str, object]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text("utf-8")))


def test_the_contract_declares_routes_terminal_and_declared_producers() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["terminal_event"] == TERMINAL_TOPIC
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["subscribe_topics"] == [GRAPH_TOPIC, RECONCILE_TOPIC, FEED_TOPIC]
    assert bus["publish_topics"] == [TERMINAL_TOPIC]
    events = contract["published_events"]
    assert isinstance(events, list)
    assert {e["topic"] for e in events} >= {COUNT_TOPIC, CLOSE_TOPIC, TERMINAL_TOPIC}
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    assert [h["topic"] for h in routing["handlers"] if "topic" in h] == [
        GRAPH_TOPIC,
        RECONCILE_TOPIC,
        FEED_TOPIC,
    ]


def test_every_routed_handler_and_model_imports() -> None:
    contract = _contract()
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    for entry in routing["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        assert hasattr(module, entry["handler"]["name"])
        for key in ("input_model", "output_model"):
            module_name, _, name = entry[key].rpartition(".")
            assert hasattr(importlib.import_module(module_name), name)


def test_the_node_is_registered_as_an_entry_point() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_seam_conservation_compute"].load() is node_package
