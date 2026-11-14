# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_start_path_census_compute declares its completed event (OMN-22940)."""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_start_path_census_compute/contract.yaml"
)


def test_start_path_census_completed_event_is_the_terminal_and_published_topic() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text())

    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.start-path-census-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.start-path-census-completed.v1"
    ]
