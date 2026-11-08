# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six INSUFFICIENT emitter scaffolds: declared producers that publish nothing yet."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
SCAFFOLDS = (
    "node_view_render_web_effect",
    "node_view_render_text_effect",
    "node_view_render_slack_effect",
    "node_view_app_interface_effect",
    "node_view_receipt_endpoint_effect",
    "node_view_status_line_receipt_effect",
)


@pytest.mark.parametrize("node", SCAFFOLDS)
def test_a_scaffold_declares_the_family_topics_as_produced_and_publishes_none(
    node: str,
) -> None:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    assert contract["event_bus"]["publish_topics"] == []
    assert [e["topic"] for e in contract["published_events"]] == [
        "onex.evt.omnimarket.view-seam-counted.v1",
        "onex.evt.omnimarket.view-seam-window-closed.v1",
    ]


@pytest.mark.parametrize("node", SCAFFOLDS)
def test_a_scaffold_handler_and_entry_point_resolve(node: str) -> None:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    module = importlib.import_module(contract["handler"]["module"])
    assert hasattr(module, contract["handler"]["class"])
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered[node].load() is importlib.import_module(
        f"omnimarket.nodes.{node}"
    )
