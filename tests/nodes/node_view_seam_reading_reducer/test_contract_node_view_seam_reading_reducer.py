# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_view_seam_reading_reducer: a pure fold with one writer, the exposure and no terminal of its own."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_view_seam_reading_reducer as node_package

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


def test_the_contract_subscribes_to_the_family_and_publishes_nothing() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text("utf-8"))
    assert contract["node_type"] == "reducer"
    assert contract["event_bus"]["publish_topics"] == []
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.view-seam-counted.v1",
        "onex.evt.omnimarket.view-seam-window-closed.v1",
        "onex.evt.omnimarket.view-seam-reading-recorded.v1",
    ]
    assert "terminal_event" not in contract
    assert contract["projection_api"]["table"] == "view_seam_readings"


def test_the_node_is_registered_as_an_entry_point() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_view_seam_reading_reducer"].load() is node_package
