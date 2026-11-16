# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key-state publish budget counts only time spent waiting on the bus (OMN-24727).

A provider call that is slow to refuse a key must not spend the request's wait on the bus, and
a publication cancelled mid-flight must not stay recorded as in flight. Every key is a fake.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelReviewGatewayKeyEventPolicy,
    ModelReviewGatewaySetAside,
)
from tests.unit.nodes.node_claude_review_gateway_effect.test_claude_review_gateway import (
    _PAYLOAD,
    FakeCall,
    FakeEventBus,
    FakeOutcome,
    _fail,
    _ok,
    _row,
)
from tests.unit.nodes.node_claude_review_gateway_effect.test_review_gateway_key_events_omn24658 import (
    GatedBus,
    _build,
    _counts,
)

pytestmark = pytest.mark.unit
_NOW = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
_BILLING = EnumDelegationFailureClass.PROVIDER_BILLING


class SlowCall(FakeCall):
    """A provider call that answers after ``delay`` seconds (the thread sleeps, the loop runs)."""

    def __init__(self, outcomes: list[FakeOutcome], delay: float) -> None:
        super().__init__(outcomes)
        self.delay = delay

    def __call__(self, request: ModelLlmDelegationCallRequest) -> FakeOutcome:
        time.sleep(self.delay)
        return super().__call__(request)


async def test_a_slow_refusing_key_does_not_spend_the_wait_on_an_accepting_bus() -> (
    None
):
    bus = FakeEventBus()
    call = SlowCall([_fail(_BILLING), _ok()], delay=0.3)
    policy = ModelReviewGatewayKeyEventPolicy(wait_seconds=0.2, backoff_seconds=60.0)
    gateway = _build(["key-a", "key-b"], call, bus, [_NOW], policy)
    status, _ = await gateway.complete(_PAYLOAD)
    assert status == 200
    # The refusal took 0.3 s of provider time against a 0.2 s bus wait: the event is
    # still published in the same request, not left for the next one.
    assert len(bus.events) == 1
    assert _counts(gateway)["held"] == 0


async def test_many_slow_refusing_keys_still_publish_every_event_in_the_request() -> (
    None
):
    bus = FakeEventBus()
    call = SlowCall([_fail(_BILLING) for _ in range(4)] + [_ok()], delay=0.1)
    policy = ModelReviewGatewayKeyEventPolicy(wait_seconds=0.2, backoff_seconds=60.0)
    gateway = _build(["k1", "k2", "k3", "k4", "k5"], call, bus, [_NOW], policy)
    status, _ = await gateway.complete(_PAYLOAD)
    assert status == 200
    # Four refusals take 0.4 s of provider time, twice the bus wait of 0.2 s.
    assert len(bus.events) == 4
    assert _counts(gateway)["held"] == 0


async def test_a_publication_cancelled_mid_flight_is_not_left_in_flight() -> None:
    bus = GatedBus()
    policy = ModelReviewGatewayKeyEventPolicy(wait_seconds=5.0, backoff_seconds=60.0)
    gateway = _build(["key-a"], FakeCall([]), bus, [_NOW], policy)
    ref = str(_row("key-a")["api_key_ref"])
    first = ModelReviewGatewaySetAside(key_ref=ref, cause=_BILLING, set_aside_at=_NOW)
    newer = ModelReviewGatewaySetAside(
        key_ref=ref, cause=_BILLING, set_aside_at=_NOW + timedelta(seconds=1)
    )
    sender = asyncio.create_task(gateway._emit_key_event("set_aside", first))
    await asyncio.wait_for(bus.started.wait(), timeout=2)
    # A newer state replaces the one on the wire, then the request is cancelled.
    await gateway._emit_key_event("set_aside", newer)
    sender.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sender
    assert gateway._in_flight is None
    # The replaced event was never published: it is a drop, and the newer one is held.
    assert _counts(gateway) == {"held": 1, "dropped": 1}


def test_the_serving_command_comment_cites_the_ticket_that_added_the_bus_lane() -> None:
    cli = (
        Path(__file__).resolve().parents[4] / "src/omnimarket/cli/cli_secret.py"
    ).read_text(encoding="utf-8")
    assert "OMN-24577: the launchd unit (ai.omninode.review-gateway)" in cli
    assert "OMN-19998" not in cli
