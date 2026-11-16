# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: every topic the serve tick reads or publishes comes from the contract's topics block.

The serve tick reads the request topic and publishes the answer events the contract names; either side
changing shape — a topic renamed in code, a role missing from the topics block, a command_topic that
names another topic than the one read — turns this red.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_stability_lane_refresh_effect.handlers.handler_stability_lane_refresh import (
    CONTRACT,
    load_topics,
)

pytestmark = pytest.mark.unit

SERVE_ROLES = (
    "stability_refresh_requested",
    "stability_refresh_pin_accepted",
    "stability_refresh_refused",
    "stability_refresh_completed",
    "stability_refresh_failed",
)


def test_every_serve_topic_comes_from_the_contract_topics_block() -> None:
    topics = load_topics()
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    for role in SERVE_ROLES:
        assert topics[role] == contract["topics"][role], role


def test_the_contract_command_topic_equals_the_requested_topic() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    topics = load_topics()
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == topics["stability_refresh_requested"]
    )
