# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A refused or unreachable key-state topic never fails, stalls or grows the review (OMN-24658).

The live state is a topic whose ACL refuses every publication from this host's login, so these
tests keep a bus that refuses every call, and a bus that never answers. Every key is a fixed fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    KEY_EVENTS_ROUTE,
    ClaudeReviewGateway,
    build_app,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
    ModelReviewGatewayKeyEventPolicy,
    ModelReviewGatewayOverlay,
)
from tests.unit.nodes.node_claude_review_gateway_effect.test_claude_review_gateway import (
    _HOST,
    _MODEL,
    _PAYLOAD,
    _REFUSED,
    _TENANT,
    _URL,
    FakeCall,
    FakeCatalog,
    FakeEventBus,
    FakeValues,
    _fail,
    _ok,
    _row,
    _value,
)

pytestmark = pytest.mark.unit
_NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
_TTL = 17
_BILLING = EnumDelegationFailureClass.PROVIDER_BILLING


class RefusingBus(FakeEventBus):
    """The topic ACL refuses every publication; each attempt is counted."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.accepting = False

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.attempts += 1
        if not self.accepting:
            raise OSError("topic authorization failed")
        await super().publish(topic, key, value)


class SilentBus(FakeEventBus):
    """An unreachable broker: the publication never answers."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.attempts += 1
        await asyncio.sleep(3600)


def _build(
    names: list[str],
    call: FakeCall,
    bus: FakeEventBus,
    clock: list[datetime],
    policy: ModelReviewGatewayKeyEventPolicy | None = None,
) -> ClaudeReviewGateway:
    rows = [_row(name) for name in names]
    return ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog(rows),
        key_values=FakeValues([str(row["api_key_ref"]) for row in rows]),
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=_TTL),
        event_bus=bus,
        clock=lambda: clock[0],
        key_event_policy=policy,
    )


def _counts(gateway: ClaudeReviewGateway) -> dict[str, int]:
    reply = TestClient(build_app(gateway, model=_MODEL)).get(KEY_EVENTS_ROUTE)
    assert reply.status_code == 200
    return dict(reply.json())


async def test_two_dead_keys_and_a_bus_that_refuses_everything_still_answer_on_the_third() -> (
    None
):
    bus = RefusingBus()
    call = FakeCall([_fail(_BILLING), _fail(_BILLING), _ok(), _ok()])
    gateway = _build(["key-a", "key-b", "key-c"], call, bus, [_NOW])
    status, reply = await gateway.complete(_PAYLOAD)
    assert status == 200
    assert reply["choices"][0]["message"]["content"] == "ok"
    assert len(call.calls) == 3
    assert gateway.set_aside == {
        str(_row("key-a")["api_key_ref"]),
        str(_row("key-b")["api_key_ref"]),
    }
    # The two set-aside events are queued, not lost, and the count is readable off the log.
    counts = _counts(gateway)
    assert counts == {"held": 2, "dropped": 0}
    # The topic is fixed: the next call replays both, in order, with their first ids.
    bus.accepting = True
    assert (await gateway.complete(_PAYLOAD))[0] == 200
    events = [json.loads(value) for _, _, value in bus.events]
    assert [(e["key_ref"], e["transition"]) for e in events] == [
        (str(_row("key-a")["api_key_ref"]), "set_aside"),
        (str(_row("key-b")["api_key_ref"]), "set_aside"),
    ]
    assert _counts(gateway)["held"] == 0


async def test_a_topic_that_stays_refused_holds_one_event_per_key_and_counts_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [_NOW]
    bus = RefusingBus()
    cycles = 40
    call = FakeCall([_fail(_BILLING) for _ in range(2 + 2 * cycles)])
    gateway = _build(["key-a", "key-b"], call, bus, clock)
    with caplog.at_level(logging.INFO):
        assert (await gateway.complete(_PAYLOAD))[0] == 503
        for _ in range(cycles):
            clock[0] += timedelta(seconds=_TTL)
            assert (await gateway.complete(_PAYLOAD))[0] == 503
    # 2 set-aside events, then a retry and a set-aside per key per expiry cycle: 162 in all.
    emitted = 2 + 4 * cycles
    counts = _counts(gateway)
    assert counts["held"] == 2
    assert counts["held"] + counts["dropped"] == emitted
    warnings = [r for r in caplog.records if "publication deferred" in r.getMessage()]
    assert len(warnings) == 1
    for key in ("key-a", "key-b"):
        assert str(_row(key)["api_key_ref"]) not in caplog.text
        assert _value(str(_row(key)["api_key_ref"])) not in caplog.text
    bus.accepting = True
    await gateway.complete(_PAYLOAD)
    assert _counts(gateway)["held"] == 0
    assert len(bus.events) == 2


async def test_an_unreachable_bus_holds_no_request_past_the_declared_wait() -> None:
    wait = 0.2
    bus = SilentBus()
    call = FakeCall([_fail(_BILLING), _fail(_BILLING), _ok(), _ok()])
    policy = ModelReviewGatewayKeyEventPolicy(wait_seconds=wait, backoff_seconds=60.0)
    gateway = _build(["key-a", "key-b", "key-c"], call, bus, [_NOW], policy)
    started = time.monotonic()
    status, _ = await gateway.complete(_PAYLOAD)
    first = time.monotonic() - started
    assert status == 200
    # One wait of `wait` seconds, not one per set-aside: the second set-aside event and the
    # next request find the bus already known to be unreachable.
    assert first < wait * 4
    assert bus.attempts == 1
    started = time.monotonic()
    status, _ = await gateway.complete(_PAYLOAD)
    assert status == 200
    assert time.monotonic() - started < wait
    assert bus.attempts == 1
    assert _counts(gateway)["held"] == 2


async def test_concurrent_reviews_do_not_queue_behind_a_publication_in_flight() -> None:
    bus = SilentBus()
    call = FakeCall([_fail(_BILLING), _ok(), _ok(), _ok()])
    policy = ModelReviewGatewayKeyEventPolicy(wait_seconds=0.5, backoff_seconds=60.0)
    gateway = _build(["key-a", "key-b"], call, bus, [_NOW], policy)
    started = time.monotonic()
    first, second = await asyncio.gather(
        gateway.complete(_PAYLOAD), gateway.complete(_PAYLOAD)
    )
    assert first[0] == 200
    assert second[0] == 200
    assert time.monotonic() - started < 1.5
    assert bus.attempts == 1


def test_the_declared_wait_and_backoff_come_from_the_node_contract() -> None:
    gateway = _build(["key-a"], FakeCall([]), RefusingBus(), [_NOW])
    policy = gateway._publish_policy
    assert policy.wait_seconds > 0
    assert policy.backoff_seconds >= policy.wait_seconds
