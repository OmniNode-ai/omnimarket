# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_running_revision_compute: the same runtime path, a failing input each."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_dev_running_revision_compute.models import (
    EnumRunningRevisionRefusalReason,
    ModelRunningRevisionPolicy,
    ModelRunningRevisionRefused,
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


async def _send(
    policy: ModelRunningRevisionPolicy, payload: dict[str, Any]
) -> tuple[Any, UUID]:
    dispatch, transport, broker, _topics = chain(policy)
    correlation_id = uuid4()
    await transport.send(
        COMMAND_TOPIC,
        key=KEY,
        value=envelope(payload, correlation_id),
        headers={},
    )
    await dispatch.drain()
    return broker, correlation_id


async def test_contradicting_facts_are_published_as_refused(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    h1 = history("omnimarket", sha(0), (merge(1, 3000),))
    h2 = history("omnimarket", sha(0), (merge(2, 2000),))
    request = facts((b,), (h1, h2))
    payload = request.model_dump(mode="json")
    payload["histories"] = [payload["histories"][0], payload["histories"][0]]
    broker, correlation_id = await _send(policy, payload)
    assert on(broker, READ_TOPIC) == []
    (out,) = on(broker, REFUSED_TOPIC)
    assert out.correlation_id == correlation_id
    refused = ModelRunningRevisionRefused.model_validate(out.payload)
    assert refused.reason is EnumRunningRevisionRefusalReason.DUPLICATE_REPOSITORY


async def test_a_payload_missing_correlation_id_goes_to_the_dead_letter(
    policy: ModelRunningRevisionPolicy,
) -> None:
    b = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    h = history("omnimarket", sha(0), (merge(1, 3000),))
    payload = facts((b,), (h, history("omnibase_infra", sha(100), ()))).model_dump(
        mode="json"
    )
    payload.pop("correlation_id")
    broker, _cid = await _send(policy, payload)
    assert on(broker, READ_TOPIC) == []
    assert on(broker, REFUSED_TOPIC) == []
