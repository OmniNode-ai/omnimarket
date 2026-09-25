# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy effect publishes the rebuild command and returns (OMN-18143 AC3).

WHAT WAS MEASURED ON THE .201 DEV LANE, 2026-09-24T19:00Z TO 2026-09-25T06:37Z
    A real rebuild takes about 20 minutes. The effect's command arm waited up to 600 s
    for the deploy agent's answer, and the runtime abandons any dispatch after 600 s
    (OMN-19355). So the first dispatch of every real deploy command was quarantined to
    ``onex.dlq.omnibase-infra.intents.v1`` as ``dispatch_deadline_exceeded``, and
    ``node_dlq_replay_effect`` replayed it about every ten minutes: 22 of the 50 extra
    rebuild-requested publishes in that window followed such a replay (OMN-19377
    comment 2026-09-25T07:39:37Z). The serial consumer also sat on that one record for
    the whole wait, so every later deploy-publish command queued behind it.

WHAT THIS PINS
    The command arm publishes, records the command under ``ONEX_STATE_DIR`` together
    with what the rollback decision needs, and returns with no subscription open. The
    outcome is observed where it arrives: the durable ``rebuild-completed`` arm, on its
    own committed consumer group, reads the record, decides rollback, and emits the
    rolled-back fact once per correlation however often the completion is redelivered.
    Every handler here is a fresh instance, because the runtime builds one per routing
    entry and a rebuild recreates the container.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    DEFAULT_PREVIOUS_IMAGE,
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelDeployRebuildCompleted,
    ModelHealthCheck,
    ModelRedeployRolledBackEvent,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_COMPLETED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type
from tests.test_omn19377_deploy_effect_skips_repeats import _CountingBus, _envelope

# Well inside the runtime's 600 s dispatch deadline, and far above a publish plus a
# file write, so a pass cannot come from a short wait.
_DISPATCH_BUDGET_S = 5.0

_FAILING_HEALTH = ModelHealthCheck(
    service="omninode-runtime",
    endpoint="http://runtime:8085/health",
    status="fail",
    latency_ms=5000,
)


def _completion(
    correlation_id: str, *, health_checks: list[ModelHealthCheck] | None = None
) -> ModelEventEnvelope[Any]:
    """The deploy agent's completion, as the durable arm receives it."""
    return ModelEventEnvelope(
        payload=ModelDeployRebuildCompleted(
            correlation_id=correlation_id,
            status=EnumRedeployStatus.SUCCESS,
            health_checks=health_checks or [],
        ).model_dump(mode="json"),
        correlation_id=UUID(correlation_id),
        event_type=TOPIC_REBUILD_COMPLETED,
    )


async def _publish(bus: _CountingBus, envelope: ModelEventEnvelope[Any]) -> Any:
    """Dispatch one command to a fresh handler, failing if it outlives the budget."""
    try:
        return await asyncio.wait_for(
            HandlerDeployPublishMonitor(event_bus=bus).handle(envelope),
            timeout=_DISPATCH_BUDGET_S,
        )
    except TimeoutError:
        pytest.fail(
            f"the command dispatch still blocks on the deploy agent's answer past "
            f"{_DISPATCH_BUDGET_S}s; on the runtime it runs into the 600 s dispatch "
            "deadline and is quarantined to the DLQ"
        )


@pytest.mark.unit
async def test_the_command_dispatch_returns_well_inside_the_runtime_deadline() -> None:
    cid = "11111111-1111-4111-8111-111111111111"
    bus = _CountingBus()
    await bus.start()
    try:
        started = time.monotonic()
        output = await _publish(bus, _envelope(cid))
        elapsed = time.monotonic() - started
    finally:
        await bus.close()

    assert bus.commands == [cid]
    assert bus.monitor_subscriptions == 0, "the command arm opened a monitor"
    assert output.events == ()
    assert output.metrics["rebuild_published"] == 1.0
    assert elapsed < 2.0, f"the dispatch took {elapsed:.2f}s"


@pytest.mark.unit
async def test_a_failed_health_completion_after_return_emits_one_rollback() -> None:
    cid = "22222222-2222-4222-8222-222222222222"
    bus = _CountingBus()
    await bus.start()
    try:
        await _publish(bus, _envelope(cid))
        output = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _completion(cid, health_checks=[_FAILING_HEALTH])
        )
    finally:
        await bus.close()

    assert len(output.events) == 1
    rolled = output.events[0].payload
    assert isinstance(rolled, ModelRedeployRolledBackEvent)
    assert rolled.restored_image == DEFAULT_PREVIOUS_IMAGE
    assert "health" in rolled.failure_reason.lower()
    assert str(rolled.correlation_id) == cid
    assert output.metrics["rolled_back"] == 1.0


@pytest.mark.unit
async def test_a_redelivered_completion_does_not_roll_back_twice() -> None:
    """The orchestrator terminalises every rolled-back fact, so a second one is a second run."""
    cid = "33333333-3333-4333-8333-333333333333"
    bus = _CountingBus()
    await bus.start()
    try:
        await _publish(bus, _envelope(cid))
        first = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _completion(cid, health_checks=[_FAILING_HEALTH])
        )
        redelivered = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _completion(cid, health_checks=[_FAILING_HEALTH])
        )
    finally:
        await bus.close()

    assert len(first.events) == 1
    assert redelivered.events == ()
    assert redelivered.metrics["rolled_back"] == 0.0


@pytest.mark.unit
async def test_a_healthy_completion_emits_nothing() -> None:
    cid = "44444444-4444-4444-8444-444444444444"
    bus = _CountingBus()
    await bus.start()
    try:
        await _publish(bus, _envelope(cid))
        output = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _completion(cid)
        )
    finally:
        await bus.close()

    assert output.events == ()
    assert output.metrics["rebuild_completed_observed"] == 1.0


@pytest.mark.unit
async def test_a_completion_for_a_correlation_this_effect_never_published_only_observes() -> (
    None
):
    """A completion the effect did not ask for carries no rollback target of its own."""
    cid = "55555555-5555-4555-8555-555555555555"
    bus = _CountingBus()
    await bus.start()
    try:
        output = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _completion(cid, health_checks=[_FAILING_HEALTH])
        )
    finally:
        await bus.close()

    assert output.events == ()
    assert output.metrics["rebuild_completed_observed"] == 1.0


@pytest.mark.unit
async def test_a_smoke_test_command_rolls_back_on_success_without_runtime_proof() -> (
    None
):
    """The command's smoke flag and rollback target survive the return, via the record."""
    cid = "66666666-6666-4666-8666-666666666666"
    command = ModelDeployPublishCommand(
        correlation_id=UUID(cid),
        runtime_lane=EnumRuntimeLane.DEV,
        smoke_test=True,
        rollback_target="ghcr.io/omninode-ai/runtime@sha256:" + "a" * 64,
    )
    envelope = ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
    )
    bus = _CountingBus()
    await bus.start()
    try:
        await _publish(bus, envelope)
        output = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _completion(cid)
        )
    finally:
        await bus.close()

    assert len(output.events) == 1
    rolled = output.events[0].payload
    assert isinstance(rolled, ModelRedeployRolledBackEvent)
    assert "smoke" in rolled.failure_reason.lower()
    assert rolled.restored_image == command.rollback_target
