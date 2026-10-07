# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared chain, walked hop by hop, now with a consumer at the end.

    onex.evt.platform.node-heartbeat.v1
      -> node_alert_channel_liveness_effect
      -> onex.evt.omnimarket.alert-channel-liveness-checked.v1
      -> node_projection_alert_channel_liveness
      -> omninode_internal.alert_channel_liveness_verdicts
      -> onex.evt.omnimarket.projection-alert-channel-liveness-applied.v1

Each hop is asserted against the contract on the other side of it, so a rename
on either end fails here instead of leaving a subscription to nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_checker_verdict_lands_in_a_durable_row() -> None:
    producer = _contract("node_alert_channel_liveness_effect")
    consumer = _contract("node_projection_alert_channel_liveness")

    assert producer["terminal_event"] == (
        "onex.evt.omnimarket.alert-channel-liveness-checked.v1"
    )
    assert producer["terminal_event"] in consumer["event_bus"]["subscribe_topics"]
    assert [t["name"] for t in consumer["db_io"]["db_tables"]] == [
        "alert_channel_liveness_verdicts"
    ]
    assert consumer["db_io"]["db_tables"][0]["access"] == "write"


def test_the_consumer_reports_and_quarantines_on_its_own_topics() -> None:
    consumer = _contract("node_projection_alert_channel_liveness")

    assert consumer["terminal_event"] == (
        "onex.evt.omnimarket.projection-alert-channel-liveness-applied.v1"
    )
    assert consumer["terminal_event"] in consumer["event_bus"]["publish_topics"]
    assert consumer["event_bus"]["dlq_topics"] == [
        "onex.dlq.omnimarket.projection-alert-channel-liveness-malformed.v1"
    ]


def test_the_consumer_is_independent_of_the_channel_it_records() -> None:
    """Recording the verdict must not need Slack: no Slack topic, no secret."""
    consumer = _contract("node_projection_alert_channel_liveness")
    published = consumer["event_bus"]["publish_topics"]

    assert not any("slack" in topic for topic in published)
    assert "secrets" not in consumer
