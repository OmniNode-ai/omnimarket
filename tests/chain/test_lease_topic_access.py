# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests: the lease topics unauthorized or unreachable (OMN-24484, Fable review).

The dev principal had no grant on the three lease topics, so a caller on the bus can meet a broker
that refuses its publish, or a node that is not consuming. The caller must then see a typed
failure, never a reply it did not get and never a default that looks like success:

* the publish of the command is refused by the broker: ``bus.call`` raises the broker's own
  authorization error, no reply arrives, nothing rides any topic;
* nobody consumes the command topic (the node is down or not yet wired): ``bus.call`` ends in a
  ``TimeoutError`` inside the overlay's ``call_timeout_s``, with the command carried and no reply;
* the node answers a refusal but the refused terminal is unauthorized for the node: the reply never
  reaches the caller, who times out instead of reading a recorded reply.

Each carries a failing control: the same drive on the plain bus returns the reply, so the
assertion that something raised is red without the injected fault.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import ModelLeaseTable
from tests.chain.lease_chain import (
    LANDING_KIND,
    InmemoryLeaseBus,
    ModelLeaseChainOverlay,
    make_command,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
SUBJECT = "OmniNode-ai/omnimarket#24484"
HOLDER = "worker:OmniNode-ai/omnimarket#24484"
HOUR = timedelta(hours=1)
FAST = ModelLeaseChainOverlay(call_timeout_s=0.5)


class TopicAuthorizationError(Exception):
    """The broker refused the principal's grant on a topic (the produce's own error)."""


class UnauthorizedCommandBus(InmemoryLeaseBus):
    """A caller with no WRITE grant on the command topic."""

    async def _send(self, topic: str, body: bytes) -> None:
        if topic == self.topic_command:
            raise TopicAuthorizationError(f"no WRITE grant on {topic}")
        await super()._send(topic, body)


class UnauthorizedRefusedTerminalBus(InmemoryLeaseBus):
    """A node with no WRITE grant on the refused terminal."""

    async def _send(self, topic: str, body: bytes) -> None:
        if topic == self.topic_refused:
            raise TopicAuthorizationError(f"no WRITE grant on {topic}")
        await super()._send(topic, body)


class UnreachableNodeBus(InmemoryLeaseBus):
    """Nobody consumes the command topic: only the reply topics are subscribed."""

    async def start(self) -> None:
        await self.bus.start()

        async def on_reply(message: Any) -> None:
            self._answered(message.value)

        for terminal in (self.topic_recorded, self.topic_refused):
            await self.bus.subscribe(
                terminal, on_message=on_reply, group_id="lease-controller"
            )


def acquire(correlation_id: str, **fields: Any) -> ModelLeaseReduceInput:
    return make_command(
        EnumLeaseOperation.ACQUIRE,
        ModelLeaseTable(kind=LANDING_KIND),
        SUBJECT,
        NOW,
        correlation_id,
        holder_run=HOLDER,
        deadline_at=NOW + HOUR,
        **fields,
    )


async def call_once(
    bus: InmemoryLeaseBus, command: ModelLeaseReduceInput
) -> ModelLeaseReduceOutput:
    await bus.start()
    try:
        return await bus.call(command)
    finally:
        await bus.stop()


# ------------------------------------------------------------ unauthorized publish
def test_error_an_unauthorized_command_publish_raises_and_carries_nothing() -> None:
    bus = UnauthorizedCommandBus(FAST)
    with pytest.raises(TopicAuthorizationError):
        asyncio.run(call_once(bus, acquire("unauthorized-command")))
    assert bus.carried == []
    assert bus.waiting  # the call never got a reply to pop its waiter


def test_control_the_plain_bus_records_the_same_command() -> None:
    reply = asyncio.run(call_once(InmemoryLeaseBus(FAST), acquire("plain-control")))
    assert reply.status is EnumLeaseStatus.RECORDED
    assert reply.epoch == 1


# ----------------------------------------------------------- unreachable node
def test_error_an_unreachable_node_times_out_inside_the_overlay_bound() -> None:
    bus = UnreachableNodeBus(FAST)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(call_once(bus, acquire("unreachable")))
    elapsed = time.monotonic() - started
    assert elapsed < 5 * FAST.call_timeout_s
    # the command rode its topic and no terminal ever answered
    assert bus.carried == [bus.topic_command]


def test_control_a_consuming_node_answers_inside_the_same_bound() -> None:
    reply = asyncio.run(call_once(InmemoryLeaseBus(FAST), acquire("answered")))
    assert reply.status is EnumLeaseStatus.RECORDED


# ------------------------------------------------- unauthorized refused terminal
async def drive_second_acquire(bus: InmemoryLeaseBus) -> ModelLeaseReduceOutput:
    """acquire twice on the same table: the second is refused (held)."""
    await bus.start()
    try:
        first = await bus.call(acquire("first"))
        assert first.status is EnumLeaseStatus.RECORDED
        return await bus.call(_second(first))
    finally:
        await bus.stop()


def _second(first: ModelLeaseReduceOutput) -> ModelLeaseReduceInput:
    return make_command(
        EnumLeaseOperation.ACQUIRE,
        first.table,
        SUBJECT,
        NOW + timedelta(minutes=1),
        "second",
        holder_run=HOLDER,
        deadline_at=NOW + HOUR,
    )


def test_error_a_refusal_the_node_may_not_publish_never_reads_as_recorded() -> None:
    bus = UnauthorizedRefusedTerminalBus(FAST)
    with pytest.raises((TopicAuthorizationError, asyncio.TimeoutError)):
        asyncio.run(drive_second_acquire(bus))


def test_control_the_plain_bus_returns_the_refusal_to_the_caller() -> None:
    reply = asyncio.run(drive_second_acquire(InmemoryLeaseBus(FAST)))
    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.HELD
