# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_roll_request_compute: the same runtime path, a failing input each."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.events.topics import (
    DEV_ROLL_DECIDED_TOPIC_V1,
    DEV_ROLL_DECISION_REQUESTED_TOPIC_V1,
    DEV_ROLL_REFUSED_TOPIC_V1,
)
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollEventRefusal,
    EnumDevRollHealth,
    EnumDevRollRefusalReason,
    ModelDevRollDecided,
    ModelDevRollPolicy,
    ModelDevRollRecord,
    ModelDevRollRefused,
    ModelDevTipChanged,
)
from tests.unit.nodes.node_dev_roll_request_compute.chain_support import (
    KEY,
    chain,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import NOW, facts, tip

pytestmark = pytest.mark.unit


async def _send(
    policy: ModelDevRollPolicy, payload: dict[str, Any]
) -> tuple[Any, UUID]:
    dispatch, transport, broker, _topics = chain(policy)
    correlation_id = uuid4()
    await transport.send(
        DEV_ROLL_DECISION_REQUESTED_TOPIC_V1,
        key=KEY,
        value=envelope(payload, correlation_id),
        headers={},
    )
    await dispatch.drain()
    return broker, correlation_id


async def test_contradicting_facts_are_published_as_refused(
    policy: ModelDevRollPolicy,
) -> None:
    last = ModelDevRollRecord(
        job_id="j",
        git_ref="c" * 40,
        runtime_started_at=NOW - timedelta(seconds=100),
        finished_at=NOW + timedelta(seconds=100),
        health=EnumDevRollHealth.HEALTHY,
        healthy_since=NOW + timedelta(seconds=100),
    )
    request = facts((tip(age_seconds=3000),), last)
    broker, correlation_id = await _send(policy, request.model_dump(mode="json"))
    assert on(broker, DEV_ROLL_DECIDED_TOPIC_V1) == []
    (out,) = on(broker, DEV_ROLL_REFUSED_TOPIC_V1)
    assert out.correlation_id == correlation_id
    refused = ModelDevRollRefused.model_validate(out.payload)
    assert refused.reason is EnumDevRollRefusalReason.INCONSISTENT_LAST_ROLL


async def test_an_unknown_repository_event_is_named_on_the_decision(
    policy: ModelDevRollPolicy,
) -> None:
    bad = tip("somewhere_else", age_seconds=3000)
    request = facts((bad,))
    broker, _cid = await _send(policy, request.model_dump(mode="json"))
    assert on(broker, DEV_ROLL_REFUSED_TOPIC_V1) == []
    (out,) = on(broker, DEV_ROLL_DECIDED_TOPIC_V1)
    decided = ModelDevRollDecided.model_validate(out.payload)
    assert [(r.event_id, r.reason) for r in decided.refused_events] == [
        (bad.event_id, EnumDevRollEventRefusal.UNKNOWN_REPOSITORY)
    ]


async def test_a_malformed_sha_never_produces_a_decision(
    policy: ModelDevRollPolicy,
) -> None:
    payload = facts((tip(age_seconds=3000),)).model_dump(mode="json")
    payload["pending_events"][0]["sha"] = "not-a-sha"
    broker, _cid = await _send(policy, payload)
    assert on(broker, DEV_ROLL_DECIDED_TOPIC_V1) == []
    assert on(broker, DEV_ROLL_REFUSED_TOPIC_V1) == []


def test_the_event_contract_refuses_a_malformed_sha_and_a_naive_time() -> None:
    good = tip(age_seconds=10).model_dump()
    with pytest.raises(ValidationError):
        ModelDevTipChanged.model_validate({**good, "sha": "abc"})
    with pytest.raises(ValidationError):
        ModelDevTipChanged.model_validate(
            {**good, "merged_at": NOW.replace(tzinfo=None)}
        )
