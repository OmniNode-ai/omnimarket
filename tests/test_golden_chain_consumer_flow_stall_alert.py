# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared chain, walked hop by hop (OMN-16778).

    onex.evt.omnimarket.projection-consumer-flow-applied.v1   (from OMN-16777)
      -> node_consumer_flow_stall_alert_effect
      -> onex.cmd.omnimarket.slack-publish.v1
      -> node_slack_publish_effect
      -> https://slack.com/api/chat.postMessage

A chain that is declared but not walked is the failure this whole epic is
about: every hop of the 2026-08-23 outage was individually green. These tests
assert the hops actually meet -- that the topic this node publishes is the topic
the publish node subscribes to, and that the payload this node builds is one the
publish node's own model accepts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers import (
    build_slack_command,
    decide_stall_alert,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    ModelConsumerFlowStallAlertRequest,
    ModelFlowWindowObservation,
    load_stall_alert_policy,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALERT_CONTRACT = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_consumer_flow_stall_alert_effect"
    / "contract.yaml"
)
_PUBLISH_CONTRACT = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_slack_publish_effect"
    / "contract.yaml"
)
_PROJECTION_CONTRACT = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "contract.yaml"
)

_EPOCH = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(minutes=1)


def _contract(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_golden_chain_reaches_slack_publish() -> None:
    """Hop 2 -> 3: the topic this node publishes is the one the publisher reads.

    Asserted against both contracts rather than a constant in this file, so a
    rename on either side fails here instead of going quiet in production.
    """
    alert = _contract(_ALERT_CONTRACT)
    publish = _contract(_PUBLISH_CONTRACT)

    alert_bus = alert["event_bus"]
    publish_bus = publish["event_bus"]
    assert isinstance(alert_bus, dict)
    assert isinstance(publish_bus, dict)

    published = set(alert_bus["publish_topics"])  # type: ignore[arg-type]
    subscribed = set(publish_bus["subscribe_topics"])  # type: ignore[arg-type]
    assert published & subscribed, (
        "the stall alert publishes nothing node_slack_publish_effect listens "
        f"to: publishes {sorted(published)}, publisher reads {sorted(subscribed)}"
    )


@pytest.mark.unit
def test_golden_chain_starts_at_the_projections_terminal_event() -> None:
    """Hop 1 -> 2: this node reads what OMN-16777 declares it emits."""
    alert = _contract(_ALERT_CONTRACT)
    projection = _contract(_PROJECTION_CONTRACT)

    alert_bus = alert["event_bus"]
    assert isinstance(alert_bus, dict)
    assert projection["terminal_event"] in set(alert_bus["subscribe_topics"])  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_publish_node_still_posts_through_chat_postmessage() -> None:
    """Hop 3 -> 4: the transport is the bot-token Web API, not a webhook.

    SLACK_WEBHOOK_URL returns HTTP 404 / no_service on both hosts (re-probed
    2026-08-27) and is being retired under OMN-15600. If the publish node ever
    moves back onto it, this alert stops being delivered and this test is the
    thing that says so.
    """
    publish = _contract(_PUBLISH_CONTRACT)
    endpoints = publish["endpoints"]
    assert isinstance(endpoints, dict)
    urls = {entry["url"] for entry in endpoints.values() if isinstance(entry, dict)}
    assert "https://slack.com/api/chat.postMessage" in urls
    secrets = publish["secrets"]
    assert isinstance(secrets, dict)
    assert "SLACK_BOT_TOKEN" in secrets
    assert "SLACK_WEBHOOK_URL" not in secrets


@pytest.mark.unit
def test_slack_command_is_accepted_by_the_publish_node() -> None:
    """The wire mirror and the consuming model have not drifted apart.

    ``ModelSlackPublish`` is ``extra="forbid"``, so a field this node invents or
    renames fails here rather than being dropped in silence on the bus -- the
    OMN-14490/OMN-14506 failure mode.
    """
    policy = load_stall_alert_policy(_ALERT_CONTRACT)
    windows = tuple(
        ModelFlowWindowObservation(
            window_start=_EPOCH + i * _WINDOW,
            window_end=_EPOCH + (i + 1) * _WINDOW,
            flow_state=EnumConsumerFlowState.STALLED,
            messages_in=15750,
            messages_out=0,
            messages_dlq=16,
            handler_errors=16,
        )
        for i in range(policy.confirm_windows)
    )
    correlation_id = uuid4()
    decision = decide_stall_alert(
        ModelConsumerFlowStallAlertRequest(
            consumer_group="node_gateway_link_health_projection_compute",
            topic="onex.cmd.omnibase-infra.gateway-link-health-upsert.v1",
            correlation_id=correlation_id,
            windows=windows,
            policy=policy,
        )
    )
    assert decision.alert is not None
    assert decision.idempotency_key is not None
    command = build_slack_command(
        payload=decision.alert,
        channel="C08PRL6BRQE",
        idempotency_key=decision.idempotency_key,
        correlation_id=correlation_id,
    )

    accepted = ModelSlackPublish.model_validate(command.model_dump(mode="json"))
    assert accepted.channel == command.channel
    assert accepted.text == command.text
    assert accepted.idempotency_key == command.idempotency_key
    assert accepted.correlation_id == correlation_id


_SLACK_PUBLISH_COMMAND = "onex.cmd.omnimarket.slack-publish.v1"
_STALL_ALERT_TERMINAL = "onex.evt.omnimarket.consumer-flow-stall-alert-evaluated.v1"


@pytest.mark.unit
def test_the_node_emits_exactly_the_two_topics_it_declares() -> None:
    """Both declared outputs are pinned by name, not just by cross-reference.

    A node that declares an output nothing ever asserts on is a node whose
    chain can be renamed into silence -- which is the whole failure class this
    epic exists to close.
    """
    alert = _contract(_ALERT_CONTRACT)
    bus = alert["event_bus"]
    assert isinstance(bus, dict)

    assert set(bus["publish_topics"]) == {  # type: ignore[arg-type]
        _SLACK_PUBLISH_COMMAND,
        _STALL_ALERT_TERMINAL,
    }
    assert alert["terminal_event"] == _STALL_ALERT_TERMINAL
    assert set(alert["externally_consumed_topics"]) == {_STALL_ALERT_TERMINAL}  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_slack_command_topic_is_the_publish_nodes_command_topic() -> None:
    """The alert reaches Slack through the canonical publisher, not a new path.

    OMN-13733 consolidated every alert caller onto node_slack_publish_effect;
    a second alerting path is exactly what that ticket removed.
    """
    publish = _contract(_PUBLISH_CONTRACT)
    dispatch = publish["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert dispatch["command_topic"] == _SLACK_PUBLISH_COMMAND
