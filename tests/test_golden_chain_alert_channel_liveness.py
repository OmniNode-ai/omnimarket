# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared chain, walked hop by hop (OMN-15600).

    onex.evt.platform.node-heartbeat.v1        (the runtime already emits it)
      -> node_alert_channel_liveness_effect
      -> https://slack.com/api/auth.test          (read-only)
      -> https://slack.com/api/conversations.info (read-only)
      -> onex.evt.omnimarket.alert-channel-liveness-checked.v1

Every hop of the 2026-08-23 outage was individually green, so a chain that is
declared but never walked is exactly the failure epic OMN-16776 exists to close.
These tests assert the hops actually meet: that the carrier this node subscribes
to is the one the platform emits, that the credential it probes is the same one
the delivery node uses, and that the verdict leaves on a surface which is not
the channel being judged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_LIVENESS_CONTRACT = _NODES / "node_alert_channel_liveness_effect" / "contract.yaml"
_PUBLISH_CONTRACT = _NODES / "node_slack_publish_effect" / "contract.yaml"
_FLOW_PROJECTION_CONTRACT = _NODES / "node_projection_consumer_flow" / "contract.yaml"


def _contract(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_rides_the_existing_heartbeat() -> None:
    """Hop 1 -> 2: the carrier is a topic the platform already publishes.

    Asserted against another node's contract rather than a constant in this
    file, so a rename on the heartbeat topic fails here instead of leaving this
    node subscribed to nothing and reporting nothing forever — which is
    indistinguishable, from the outside, from a permanently healthy channel.
    """
    liveness = _contract(_LIVENESS_CONTRACT)
    flow = _contract(_FLOW_PROJECTION_CONTRACT)

    subscribed = set(liveness["event_bus"]["subscribe_topics"])
    already_carried = set(flow["event_bus"]["subscribe_topics"])
    assert subscribed & already_carried, (
        "the liveness check subscribes to a topic no shipped node reads, so "
        f"there is no evidence it is carried: {sorted(subscribed)}"
    )


def test_the_verdict_leaves_on_a_surface_that_is_not_the_channel() -> None:
    """Hop 4: a Slack outage must not be able to suppress the report of it.

    "The only thing that would tell you alerting is broken is the alerting" is
    the sentence this ticket was filed over. The terminal event is on the bus,
    not in Slack.
    """
    liveness = _contract(_LIVENESS_CONTRACT)
    terminal = liveness["terminal_event"]
    assert terminal == "onex.evt.omnimarket.alert-channel-liveness-checked.v1"
    assert terminal in set(liveness["event_bus"]["publish_topics"])
    assert terminal in set(liveness["externally_consumed_topics"])
    assert "slack" not in terminal


def test_the_carrier_and_the_dlq_are_the_topics_this_node_declares() -> None:
    """The three topic names, asserted literally.

    Named rather than derived so a rename anywhere in the chain has to be made
    deliberately in two places. A liveness checker silently rewired to a topic
    nobody publishes reports nothing forever, which from the outside is
    indistinguishable from a permanently healthy channel.
    """
    liveness = _contract(_LIVENESS_CONTRACT)
    assert set(liveness["event_bus"]["subscribe_topics"]) == {
        "onex.evt.platform.node-heartbeat.v1"
    }
    assert set(liveness["event_bus"]["publish_topics"]) == {
        "onex.evt.omnimarket.alert-channel-liveness-checked.v1"
    }
    assert set(liveness["event_bus"]["dlq_topics"]) == {
        "onex.dlq.omnimarket.alert-channel-liveness-malformed.v1"
    }


def test_the_probe_judges_the_same_credential_that_delivers() -> None:
    """Probing a different credential from the delivering one proves nothing."""
    liveness = _contract(_LIVENESS_CONTRACT)
    publish = _contract(_PUBLISH_CONTRACT)
    assert "SLACK_BOT_TOKEN" in liveness["secrets"]
    assert "SLACK_BOT_TOKEN" in publish["secrets"]


def test_the_retired_webhook_is_declared_nowhere_in_this_chain() -> None:
    """``SLACK_WEBHOOK_URL`` is dead (HTTP 404 / no_service) and stays retired.

    If either node ever re-declares it as a secret or an endpoint, this
    liveness check would be proving a channel that is not the one alerts travel
    through. Asserted against the declaration blocks rather than the whole file
    so the prose explaining *why* it is retired does not trip its own rule.
    """
    for path in (_LIVENESS_CONTRACT, _PUBLISH_CONTRACT):
        contract = _contract(path)
        assert "SLACK_WEBHOOK_URL" not in contract["secrets"]
        for spec in contract["endpoints"].values():
            assert "hooks.slack.com" not in spec["url"]


def test_every_declared_endpoint_is_read_only() -> None:
    """The checker cannot post into the channel it judges.

    A canary written into the alert channel every interval is how a channel
    gets muted — the OMN-14440 precedent, which fired every 30 minutes for
    three months into something nobody read.
    """
    liveness = _contract(_LIVENESS_CONTRACT)
    for name, spec in liveness["endpoints"].items():
        assert spec["method"] == "GET", f"{name} is not a read-only probe"
        assert "chat.postMessage" not in spec["url"]


def test_the_contract_endpoints_match_the_service_endpoint_authority() -> None:
    """The URLs the prober imports are the URLs the contract declares."""
    from omnimarket.config.service_endpoints import (
        SLACK_AUTH_TEST_URL,
        SLACK_CONVERSATIONS_INFO_URL,
    )

    liveness = _contract(_LIVENESS_CONTRACT)
    declared = {spec["url"] for spec in liveness["endpoints"].values()}
    assert declared == {SLACK_AUTH_TEST_URL, SLACK_CONVERSATIONS_INFO_URL}


def test_the_node_declares_a_dlq_and_does_not_become_a_new_silent_loss_site() -> None:
    """This node exists because a failure went unseen; it must not add one."""
    liveness = _contract(_LIVENESS_CONTRACT)
    assert liveness["event_bus"]["dlq_topics"]
