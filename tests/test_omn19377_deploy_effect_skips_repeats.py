# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy effect answers a repeat of a correlation the agent already answered (OMN-19377).

WHAT WAS MEASURED ON THE .201 DEV LANE, 2026-09-23
    The deploy effect consumed 53,016 ``redeploy-deploy-publish`` copies of one gate
    decision (correlation ``2d3ccad1``) and put every one of them through a full
    deploy-agent round trip: two fresh subscriptions, one signed publish to
    ``onex.cmd.deploy.rebuild-requested.v1`` and a wait for the answer. The agent
    rejected 52,398 of them as ``duplicate``. The effect consumes serially, so the seven
    real dev-lane commands queued behind the copies waited 3h18m to 5h14m. Before the
    OMN-19242 AC1 fix each copy cost the full 600 s timeout; after it, a median 134 ms.

WHAT THIS PINS
    Once the agent has answered a correlation with anything but ``busy``, the answer
    to a repeat is already known: the agent keeps a record of every command it ran or
    refused, and refuses a repeat as ``duplicate``. The effect answers the repeat
    itself, with no publish and no subscription. ``busy`` is the one answer that keeps
    no record, and a command the effect never heard back about may still be waiting in
    the agent's queue, so neither is remembered.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    EnumDeployRejectionReason,
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_COMPLETED,
    TOPIC_REBUILD_REJECTED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type

# The timeout the effect ran with on the lane. A repeat that returns quickly only
# because the timeout is short would prove nothing.
_PRODUCTION_TIMEOUT_S = 600.0

_LIVE_CORRELATION = "2d3ccad1-6d0d-4f61-9f1d-5c5a8bd12d90"


class _CountingBus(EventBusInmemory):
    """The in-memory bus, counting rebuild commands and monitor subscriptions."""

    def __init__(self, *, fail_first_command_publish: bool = False) -> None:
        super().__init__(environment="test", group="omn19377")
        self.commands: list[str] = []
        self.monitor_subscriptions = 0
        self._fail_next_command_publish = fail_first_command_publish

    async def publish(self, topic: str, key: Any, value: Any, **kwargs: Any) -> Any:
        if topic == TOPIC_REBUILD_REQUESTED:
            if self._fail_next_command_publish:
                self._fail_next_command_publish = False
                raise ConnectionError("broker unavailable")
            self.commands.append(json.loads(value)["correlation_id"])
        return await super().publish(topic, key, value, **kwargs)

    async def subscribe(self, topic: str, **kwargs: Any) -> Any:
        if topic in (TOPIC_REBUILD_COMPLETED, TOPIC_REBUILD_REJECTED):
            self.monitor_subscriptions += 1
        return await super().subscribe(topic, **kwargs)


async def _deploy_agent(
    bus: EventBusInmemory,
    *,
    first_answer: EnumDeployRejectionReason | None = None,
    delay_s: float = 0.01,
    silent: bool = False,
) -> None:
    """Stand in for the deploy agent's dedupe.

    The first time a correlation arrives it is completed, or refused with
    ``first_answer``. Every later arrival of it is refused as ``duplicate``, which is
    what ``JobStore.is_duplicate`` answers on the lane. ``silent`` never answers, the
    shape of a command queued behind a running job.
    """
    seen: set[str] = set()

    async def _answer(correlation_id: str, repeat: bool) -> None:
        await asyncio.sleep(delay_s)
        if repeat or first_answer is not None:
            reason = EnumDeployRejectionReason.DUPLICATE if repeat else first_answer
            assert reason is not None
            await bus.publish(
                TOPIC_REBUILD_REJECTED,
                key=correlation_id.encode(),
                value=json.dumps(
                    {
                        "correlation_id": correlation_id,
                        "reason": reason.value,
                        "scope": "full",
                    }
                ).encode(),
            )
            return
        await bus.publish(
            TOPIC_REBUILD_COMPLETED,
            key=correlation_id.encode(),
            value=json.dumps(
                {"correlation_id": correlation_id, "status": "success"}
            ).encode(),
        )

    async def _on_command(message: object) -> None:
        correlation_id = json.loads(message.value)["correlation_id"]  # type: ignore[attr-defined]
        repeat = correlation_id in seen
        seen.add(correlation_id)
        if silent:
            return
        asyncio.get_running_loop().create_task(_answer(correlation_id, repeat))

    await bus.subscribe(
        TOPIC_REBUILD_REQUESTED, on_message=_on_command, group_id="fake-deploy-agent"
    )


def _envelope(correlation_id: str) -> ModelEventEnvelope[Any]:
    command = ModelDeployPublishCommand(
        correlation_id=UUID(correlation_id), runtime_lane=EnumRuntimeLane.DEV
    )
    return ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
    )


@pytest.mark.unit
async def test_ac1_a_repeat_of_an_answered_correlation_is_not_published() -> None:
    """The live sequence: the deploy completes, then a copy of its command arrives."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        await handler.handle(_envelope(_LIVE_CORRELATION))
        subscriptions_after_first = bus.monitor_subscriptions

        started = time.monotonic()
        await handler.handle(_envelope(_LIVE_CORRELATION))
        elapsed = time.monotonic() - started
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION], (
        f"the repeat reached the agent again: {len(bus.commands)} rebuild commands"
    )
    assert bus.monitor_subscriptions == subscriptions_after_first
    assert elapsed < 1.0, f"the repeat held the effect for {elapsed:.2f}s"


@pytest.mark.unit
async def test_ac1_a_duplicate_answer_is_remembered_too() -> None:
    """After a restart the first copy reaches the agent, which says ``duplicate``.

    That answer settles the correlation as surely as a completion does, so the
    copies behind it are not sent.
    """
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, first_answer=EnumDeployRejectionReason.DUPLICATE)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        for _ in range(5):
            await handler.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION]


@pytest.mark.unit
async def test_ac2_a_backlog_of_repeats_does_not_hold_the_next_command() -> None:
    """1000 copies of one decision, then a real command: the real one goes out now."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        await handler.handle(_envelope(_LIVE_CORRELATION))

        real = str(uuid4())
        started = time.monotonic()
        for _ in range(1000):
            await handler.handle(_envelope(_LIVE_CORRELATION))
        await handler.handle(_envelope(real))
        elapsed = time.monotonic() - started
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION, real]
    assert elapsed < 5.0, f"the real command waited {elapsed:.1f}s behind the repeats"


@pytest.mark.unit
async def test_ac3_a_skipped_repeat_is_its_own_outcome() -> None:
    """Not a rejection by the agent, not a timeout, not a rollback, and no event."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        first = await handler.handle(_envelope(_LIVE_CORRELATION))
        repeat = await handler.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert first.metrics["duplicate_skipped"] == 0.0
    assert first.metrics["rebuild_success"] == 1.0
    assert repeat.events == ()
    assert repeat.metrics["duplicate_skipped"] == 1.0
    assert repeat.metrics["rebuild_rejected"] == 0.0
    assert repeat.metrics["timed_out"] == 0.0
    assert repeat.metrics["rolled_back"] == 0.0
    assert repeat.metrics["rebuild_success"] == 0.0


@pytest.mark.unit
async def test_ac4_a_command_whose_publish_raised_is_published_on_redelivery() -> None:
    """A publish that never reached the broker must not read as an answered command."""
    bus = _CountingBus(fail_first_command_publish=True)
    await bus.start()
    try:
        await _deploy_agent(bus)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        with pytest.raises(ConnectionError):
            await handler.handle(_envelope(_LIVE_CORRELATION))
        await handler.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION]


@pytest.mark.unit
async def test_a_busy_refusal_is_not_remembered() -> None:
    """``busy`` is the one answer the agent keeps no record of.

    It commits past the command without a job, so a re-publish of the same
    correlation is the only way that deploy ever runs. Skipping it would drop a real
    deploy silently.
    """
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, first_answer=EnumDeployRejectionReason.BUSY)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        await handler.handle(_envelope(_LIVE_CORRELATION))
        await handler.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION, _LIVE_CORRELATION]


@pytest.mark.unit
async def test_a_command_with_no_answer_is_not_remembered() -> None:
    """A timeout is not an answer: the command may still be queued at the agent."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, silent=True)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=0.2, poll_interval_s=0.05
        )
        await handler.handle(_envelope(_LIVE_CORRELATION))
        await handler.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION, _LIVE_CORRELATION]


@pytest.mark.unit
async def test_a_different_correlation_still_deploys() -> None:
    """The memory is per correlation, not a latch that swallows the next deploy."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        other = str(uuid4())
        await handler.handle(_envelope(_LIVE_CORRELATION))
        await handler.handle(_envelope(other))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION, other]
