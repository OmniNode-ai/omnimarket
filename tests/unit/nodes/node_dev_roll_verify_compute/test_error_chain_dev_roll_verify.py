# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_roll_verify_compute: the same runtime path, a failing input each."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevRollVerifyRefusal,
    ModelDevRollVerifyPolicy,
    ModelDevRollVerifyRefused,
    ModelDevRuntimeStarted,
)
from omnimarket.nodes.node_dev_roll_verify_compute.overlay import (
    load_dev_roll_verify_policy,
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
from tests.unit.nodes.node_dev_roll_verify_compute.conftest import (
    NOW,
    POLICY_DOC,
    facts,
    start,
    write_overlay,
)

pytestmark = pytest.mark.unit


async def _send(
    policy: ModelDevRollVerifyPolicy, payload: dict[str, Any]
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


async def test_a_start_for_another_roll_is_refused(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    verify = facts((start(job_id="job-2"),))
    broker, correlation_id = await _send(policy, verify.model_dump(mode="json"))
    assert on(broker, VERIFIED_TOPIC) == []
    (out,) = on(broker, REFUSED_TOPIC)
    assert out.correlation_id == correlation_id
    refused = ModelDevRollVerifyRefused.model_validate(out.payload)
    assert refused.reason is EnumDevRollVerifyRefusal.START_FOR_OTHER_ROLL
    assert refused.job_id == "job-1"


async def test_an_unfinished_roll_is_refused(policy: ModelDevRollVerifyPolicy) -> None:
    verify = facts((start(),), roll_finished_at=None)
    broker, correlation_id = await _send(policy, verify.model_dump(mode="json"))
    assert on(broker, VERIFIED_TOPIC) == []
    (out,) = on(broker, REFUSED_TOPIC)
    assert out.correlation_id == correlation_id
    refused = ModelDevRollVerifyRefused.model_validate(out.payload)
    assert refused.reason is EnumDevRollVerifyRefusal.ROLL_NOT_FINISHED


async def test_a_naive_started_at_never_produces_an_event(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    payload = facts((start(),)).model_dump(mode="json")
    payload["starts"][0]["started_at"] = NOW.replace(tzinfo=None).isoformat()
    broker, _cid = await _send(policy, payload)
    assert on(broker, VERIFIED_TOPIC) == []
    assert on(broker, REFUSED_TOPIC) == []


def test_the_start_contract_refuses_a_naive_time_and_an_unknown_field() -> None:
    good = start().model_dump()
    with pytest.raises(ValidationError):
        ModelDevRuntimeStarted.model_validate(
            {**good, "started_at": NOW.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError):
        ModelDevRuntimeStarted.model_validate({**good, "unknown": 1})


def test_an_overlay_without_the_declared_bound_raises(tmp_path: Path) -> None:
    doc = {
        key: value
        for key, value in POLICY_DOC.items()
        if key != "declared_starts_per_roll"
    }
    with pytest.raises(NodeOverlayError):
        load_dev_roll_verify_policy(write_overlay(tmp_path, doc))


def test_a_zero_declared_starts_per_roll_is_refused_by_the_policy() -> None:
    with pytest.raises(ValidationError):
        ModelDevRollVerifyPolicy.model_validate(
            {**POLICY_DOC, "declared_starts_per_roll": 0}
        )
