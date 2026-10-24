# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing mirror declares its subscribed topics for provisioning (OMN-21340)."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_landing_subscriptions_are_declared_for_provisioning() -> None:
    contract_path = (
        Path(__file__).parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_work_ledger_landing_mirror"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    subscribed_topics = set(contract["runtime_dispatch"]["subscribe_topics"])
    consumed_topics = {event["topic"] for event in contract["consumed_events"]}

    assert subscribed_topics <= consumed_topics
    assert "event_bus" not in contract
    assert "handler_routing" not in contract
