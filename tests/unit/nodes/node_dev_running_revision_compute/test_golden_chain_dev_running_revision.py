# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_running_revision_compute over its real contract on the in-memory bus."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.models.running_revision import (
    EnumRunningRevisionStatus,
    ModelRunningRevisionPolicy,
    ModelRunningRevisionReading,
)
from tests.unit.nodes.node_dev_running_revision_compute.chain_support import (
    COMMAND_TOPIC,
    KEY,
    READ_TOPIC,
    REFUSED_TOPIC,
    chain,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_running_revision_compute.conftest import (
    build,
    facts,
    history,
    merge,
    sha,
)

pytestmark = pytest.mark.unit


def test_the_subscribed_topic_is_the_declared_command_topic(
    policy: ModelRunningRevisionPolicy,
) -> None:
    _dispatch, _transport, _broker, topics = chain(policy)
    assert topics == {COMMAND_TOPIC}


async def test_a_current_facts_message_is_published_as_read(
    policy: ModelRunningRevisionPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    correlation_id = uuid4()
    b = build("b1", 60, {"omnimarket": 3, "omnibase_infra": 100})
    h = history("omnimarket", sha(0), (merge(1, 3000), merge(2, 2000), merge(3, 1000)))
    request = facts((b,), (h, history("omnibase_infra", sha(100), ())))
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(request.model_dump(mode="json"), correlation_id),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, READ_TOPIC)
    assert out.correlation_id == correlation_id
    reading = ModelRunningRevisionReading.model_validate(out.payload)
    assert reading.status is EnumRunningRevisionStatus.CURRENT
    assert on(broker, REFUSED_TOPIC) == []


async def test_a_behind_facts_message_is_published_as_read(
    policy: ModelRunningRevisionPolicy,
) -> None:
    dispatch, transport, broker, _topics = chain(policy)
    correlation_id = uuid4()
    b = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    h = history(
        "omnimarket",
        sha(0),
        (merge(1, 5400), merge(2, 5000), merge(3, 4000), merge(4, 3000)),
    )
    request = facts((b,), (h, history("omnibase_infra", sha(100), ())))
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(request.model_dump(mode="json"), correlation_id),
        headers={},
    )
    assert await dispatch.drain() == 1
    (out,) = on(broker, READ_TOPIC)
    assert out.correlation_id == correlation_id
    reading = ModelRunningRevisionReading.model_validate(out.payload)
    assert reading.status is EnumRunningRevisionStatus.BEHIND
    assert reading.red is True
    assert on(broker, REFUSED_TOPIC) == []
