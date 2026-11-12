# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_roll_verify_compute over its real contract on the in-memory bus.

Topic bytes in, typed verdict out, through the runtime's own path: discovery reads
``contract.yaml``, ``build_routing_map`` resolves the command topic to the handler and its
``event_model``, ``RuntimeDispatch`` decodes the envelope and coerces the payload into
``ModelDevRollVerifyFacts``, and the verdict goes out on the topics ``terminal_events`` name.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevRollVerdict,
    ModelDevRollVerified,
    ModelDevRollVerifyPolicy,
)
from tests.unit.nodes.node_dev_roll_verify_compute.chain_support import (
    COMMAND_TOPIC,
    KEY,
    REFUSED_TOPIC,
    VERIFIED_TOPIC,
    chain,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_roll_verify_compute.conftest import facts, start

pytestmark = pytest.mark.unit


def test_the_subscribed_topic_is_the_declared_command_topic(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    _dispatch, _transport, _broker, topics = chain(policy)
    assert topics == {COMMAND_TOPIC}


async def test_golden_passed_roll_is_published_as_verified(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    correlation_id = uuid4()
    verify = facts((start(),))
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(verify.model_dump(mode="json"), correlation_id),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, VERIFIED_TOPIC)
    assert out.correlation_id == correlation_id
    verified = ModelDevRollVerified.model_validate(out.payload)
    assert verified.verdict is EnumDevRollVerdict.PASSED
    assert verified.starts_counted == 1
    assert verified.declared_starts == 1
    assert on(broker, REFUSED_TOPIC) == []


async def test_golden_two_start_roll_is_verified_too_many_starts(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    verify = facts(
        (start(seconds_after_roll_start=60), start(seconds_after_roll_start=90))
    )
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(verify.model_dump(mode="json"), uuid4()),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, VERIFIED_TOPIC)
    verified = ModelDevRollVerified.model_validate(out.payload)
    assert verified.verdict is EnumDevRollVerdict.TOO_MANY_STARTS
    assert verified.starts_counted == 2
    assert verified.declared_starts == 1
    assert verified.reapply_starts == 0
    assert on(broker, REFUSED_TOPIC) == []


async def test_golden_unreadable_starts_arrive_as_start_count_unknown(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    verify = facts(None)
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(verify.model_dump(mode="json"), uuid4()),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, VERIFIED_TOPIC)
    verified = ModelDevRollVerified.model_validate(out.payload)
    assert verified.verdict is EnumDevRollVerdict.START_COUNT_UNKNOWN
    assert verified.starts_counted is None
    assert verified.declared_starts == 1
    assert on(broker, REFUSED_TOPIC) == []
