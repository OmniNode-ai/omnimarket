# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dispatch_reading_compute publishes its declared terminal event."""

from __future__ import annotations

from importlib.resources import files

import pytest
import yaml

NODE = "node_dispatch_reading_compute"
COMPUTED_TOPIC = "onex.evt.omnimarket.dispatch-reading-computed.v1"


@pytest.mark.unit
def test_node_dispatch_reading_compute_publishes_computed_event() -> None:
    contract = yaml.safe_load(
        (files("omnimarket.nodes") / NODE / "contract.yaml").read_text()
    )

    assert contract["name"] == NODE
    assert contract["event_bus"]["publish_topics"] == [COMPUTED_TOPIC]
    assert contract["terminal_event"] == COMPUTED_TOPIC
