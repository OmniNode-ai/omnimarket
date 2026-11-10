# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_roll_request_compute over its real contract on the in-memory bus.

Topic bytes in, typed decision out, through the runtime's own path: discovery reads
``contract.yaml``, ``build_routing_map`` resolves the command topic to the handler and its
``event_model``, ``RuntimeDispatch`` decodes the envelope and coerces the payload into
``ModelDevRollFacts``, and the decision goes out on the topic ``published_events`` names.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollAction,
    EnumDevRollHealth,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollPolicy,
)
from tests.unit.nodes.node_dev_roll_request_compute.chain_support import (
    COMMAND_TOPIC,
    DECIDED_TOPIC,
    KEY,
    REFUSED_TOPIC,
    chain,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import facts, roll, tip

pytestmark = pytest.mark.unit


def test_the_subscribed_topic_is_the_declared_command_topic(
    policy: ModelDevRollPolicy,
) -> None:
    _dispatch, _transport, _broker, topics = chain(policy)
    assert topics == {COMMAND_TOPIC}


async def test_golden_roll_now_is_published_as_decided(
    policy: ModelDevRollPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    correlation_id = uuid4()
    request = facts(tuple(tip(age_seconds=1900 + n) for n in range(5)))
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(request.model_dump(mode="json"), correlation_id),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, DECIDED_TOPIC)
    assert out.correlation_id == correlation_id
    decided = ModelDevRollDecided.model_validate(out.payload)
    assert decided.action is EnumDevRollAction.ROLL_NOW
    assert decided.shadow is True
    assert len(decided.rolled_event_ids) == 5
    assert on(broker, REFUSED_TOPIC) == []


async def test_golden_hold_is_published_as_decided(policy: ModelDevRollPolicy) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    request = facts((tip(age_seconds=30),))
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(request.model_dump(mode="json"), uuid4()),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, DECIDED_TOPIC)
    decided = ModelDevRollDecided.model_validate(out.payload)
    assert decided.action is EnumDevRollAction.HOLD
    assert decided.hold_reason is EnumDevRollHoldReason.WINDOW_OPEN
    assert decided.would_request is None


async def test_golden_failed_roll_past_the_bound_is_released_and_named(
    policy: ModelDevRollPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    failed = roll(
        started_ago=7000,
        finished_ago=3700,
        health=EnumDevRollHealth.UNHEALTHY,
        healthy_ago=None,
    )
    request = facts((tip(age_seconds=3000),), failed)
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(request.model_dump(mode="json"), uuid4()),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, DECIDED_TOPIC)
    decided = ModelDevRollDecided.model_validate(out.payload)
    assert decided.action is EnumDevRollAction.ROLL_NOW
    assert decided.released_failed_roll_job_id == "job-1"
    assert on(broker, REFUSED_TOPIC) == []
