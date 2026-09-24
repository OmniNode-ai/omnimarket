# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A redelivered gate decision cannot hold the deploy effect for ten minutes (OMN-19242).

WHAT WAS MEASURED ON THE .201 DEV LANE, 2026-09-23
    One allowed gate decision (envelope ``e8fbd49f``, correlation ``2d3ccad1``) was
    dead-lettered by a failing projection fold and replayed about 22,900 times. Each
    replay reached the redeploy orchestrator, which published a fresh
    ``redeploy-deploy-publish`` command for a run whose deploy it had already
    published. The deploy agent correctly rejected every one as ``duplicate``, about a
    second after it arrived, but the deploy effect ignored the rejection and waited the
    full 600-second timeout each time. The rebuild-requested / rebuild-rejected pairs
    landed exactly ten minutes apart, the effect's consumer group went Empty with a lag
    near 19,700, and a real redeploy queued behind the duplicates was unreachable.

TWO DEFECTS, ONE TEST EACH
    AC1: the publish-monitor ends its wait when a rejection for its OWN correlation
    arrives, and returns a rejected outcome rather than a timeout. A timeout also
    reads as a rollback trigger, and a rejected command never went live.

    AC2: the orchestrator publishes one deploy command per decision identity. A
    redelivery of the same gate decision emits nothing.
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
    EnumRedeployStatus,
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_REJECTED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
    _rollback_reason,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    HandlerRedeployOrchestrator,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type

# The timeout the effect ran with on the lane. A test that passes only because the
# timeout is short would prove nothing, so the monitor keeps the production value.
_PRODUCTION_TIMEOUT_S = 600.0

# The live gate decision, verbatim: the ``payload`` of envelope e8fbd49f as read off
# onex.dlq.omnibase-infra.events.v1 on the .201 dev lane (original offset 25384).
_LIVE_CORRELATION = "2d3ccad1-6d0d-4f61-9f1d-5c5a8bd12d90"
_LIVE_DECISION: dict[str, Any] = {
    "allowed": True,
    "image_digest": None,
    "rollback_target": "omninode-runtime:v2.3.1",
    "reason": "dev lane is not gated; deploy may proceed",
    "deploy_context": {
        "scope": "full",
        "git_ref": "c159b711808d1eca01accabe8e3b1f3db3f3cdf9",
        "runtime_lane": "dev",
        "build_source": "workspace",
        "services": [],
        "image_ref": None,
        "image_digest": None,
        "promotion_batch_id": None,
        "requested_by": "gha/omnibase_infra/pr-3996",
        "smoke_test": False,
        "previous_image": "omninode-runtime:v2.3.1",
        "rollback_target": None,
    },
    "outcome": "allowed_lane_not_gated",
    "grant_id": None,
    "requested_image_digest": None,
    "evaluated_at": None,
    "correlation_id": _LIVE_CORRELATION,
}


async def _rejecting_agent(
    bus: EventBusInmemory,
    *,
    reason: EnumDeployRejectionReason,
    delay_s: float,
    correlation_override: str | None = None,
) -> None:
    """Stand in for the deploy agent: reject every command after ``delay_s``."""

    async def _reject_later(correlation_id: str) -> None:
        await asyncio.sleep(delay_s)
        rejection = {
            "correlation_id": correlation_override or correlation_id,
            "reason": reason.value,
            "scope": "full",
        }
        await bus.publish(
            TOPIC_REBUILD_REJECTED,
            key=correlation_id.encode(),
            value=json.dumps(rejection).encode(),
        )

    async def _on_command(message: object) -> None:
        payload = json.loads(message.value)  # type: ignore[attr-defined]
        asyncio.get_running_loop().create_task(_reject_later(payload["correlation_id"]))

    await bus.subscribe(
        TOPIC_REBUILD_REQUESTED, on_message=_on_command, group_id="fake-deploy-agent"
    )


@pytest.mark.unit
async def test_ac1_a_rejection_for_its_own_correlation_ends_the_wait() -> None:
    """The live sequence: publish, agent rejects as duplicate one second later."""
    bus = EventBusInmemory(environment="test", group="omn19242")
    await bus.start()
    try:
        await _rejecting_agent(
            bus, reason=EnumDeployRejectionReason.DUPLICATE, delay_s=1.0
        )
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )

        started = time.monotonic()
        result = await handler.publish_and_monitor(
            ModelDeployPublishCommand(
                correlation_id=UUID(_LIVE_CORRELATION),
                runtime_lane=EnumRuntimeLane.DEV,
            )
        )
        elapsed = time.monotonic() - started
    finally:
        await bus.close()

    assert elapsed < 5.0, f"waited {elapsed:.1f}s after the agent rejected"
    assert result.timed_out is False
    assert result.success is False
    assert result.status == EnumRedeployStatus.FAILED
    assert result.rejection_reason == EnumDeployRejectionReason.DUPLICATE
    assert "duplicate" in result.errors[0]
    # A rejected command never went live, so there is nothing to roll back. A
    # timeout, which is what this used to return, IS a rollback trigger.
    assert _rollback_reason(result, smoke_test=False) is None


@pytest.mark.unit
async def test_ac1_a_rejection_for_another_correlation_does_not_end_the_wait() -> None:
    """Only this command's own rejection counts: a peer's must not cut it short."""
    bus = EventBusInmemory(environment="test", group="omn19242")
    await bus.start()
    try:
        await _rejecting_agent(
            bus,
            reason=EnumDeployRejectionReason.DUPLICATE,
            delay_s=0.05,
            correlation_override=str(uuid4()),
        )
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=1.0, poll_interval_s=0.05
        )
        result = await handler.publish_and_monitor(
            ModelDeployPublishCommand(
                correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.DEV
            )
        )
    finally:
        await bus.close()

    assert result.timed_out is True
    assert result.rejection_reason is None


@pytest.mark.unit
async def test_ac1_the_command_arm_reports_the_rejection_and_rolls_nothing_back() -> (
    None
):
    """Through ``handle``, the entry the runtime calls: fast, no rollback event."""
    bus = EventBusInmemory(environment="test", group="omn19242")
    await bus.start()
    try:
        await _rejecting_agent(
            bus, reason=EnumDeployRejectionReason.DUPLICATE, delay_s=1.0
        )
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        command = ModelDeployPublishCommand(
            correlation_id=UUID(_LIVE_CORRELATION), runtime_lane=EnumRuntimeLane.DEV
        )
        started = time.monotonic()
        output = await handler.handle(
            ModelEventEnvelope(
                payload=command,
                correlation_id=command.correlation_id,
                event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
            )
        )
        elapsed = time.monotonic() - started
    finally:
        await bus.close()

    assert elapsed < 5.0
    assert output.events == ()
    assert output.metrics["rebuild_rejected"] == 1.0
    assert output.metrics["timed_out"] == 0.0
    assert output.metrics["rolled_back"] == 0.0


def _gate_evaluated(correlation_id: str = _LIVE_CORRELATION) -> ModelEventEnvelope[Any]:
    decision = dict(_LIVE_DECISION)
    decision["correlation_id"] = correlation_id
    return ModelEventEnvelope(
        payload=decision,
        correlation_id=UUID(correlation_id),
        event_type="omnimarket.prod-promotion-gate-evaluated",
    )


def _deploy_publishes(outputs: list[Any]) -> list[Any]:
    return [
        event
        for output in outputs
        for event in output.events
        if event.event_type == TOPIC_DEPLOY_PUBLISH
    ]


@pytest.mark.unit
async def test_ac2_a_redelivered_gate_decision_publishes_one_deploy() -> None:
    """The same gate-evaluated envelope twice yields exactly one deploy command."""
    orchestrator = HandlerRedeployOrchestrator()
    envelope = _gate_evaluated()

    outputs = [
        await orchestrator.handle(envelope),
        await orchestrator.handle(envelope),
    ]

    published = _deploy_publishes(outputs)
    assert len(published) == 1
    assert str(published[0].payload.correlation_id) == _LIVE_CORRELATION
    assert outputs[1].events == ()


@pytest.mark.unit
async def test_ac2_a_replayed_copy_with_a_fresh_envelope_is_the_same_decision() -> None:
    """The DLQ replay loop re-wraps the payload; the decision identity is unchanged."""
    orchestrator = HandlerRedeployOrchestrator()

    outputs = [await orchestrator.handle(_gate_evaluated()) for _ in range(50)]

    assert len(_deploy_publishes(outputs)) == 1


@pytest.mark.unit
async def test_ac2_a_different_run_still_deploys() -> None:
    """Dedupe is per decision, not a global latch that swallows the next deploy."""
    orchestrator = HandlerRedeployOrchestrator()

    outputs = [
        await orchestrator.handle(_gate_evaluated()),
        await orchestrator.handle(_gate_evaluated(str(uuid4()))),
    ]

    assert len(_deploy_publishes(outputs)) == 2


@pytest.mark.unit
async def test_ac2_a_decision_that_fails_to_build_is_not_remembered() -> None:
    """A decision that raises must not be recorded as published.

    Otherwise its corrected redelivery would be silently dropped as a duplicate of a
    deploy that never went out.
    """
    orchestrator = HandlerRedeployOrchestrator()
    broken = dict(_LIVE_DECISION)
    broken["deploy_context"] = None
    with pytest.raises(Exception, match="neither an echoed"):
        await orchestrator.handle(
            ModelEventEnvelope(
                payload=broken,
                correlation_id=UUID(_LIVE_CORRELATION),
                event_type="omnimarket.prod-promotion-gate-evaluated",
            )
        )

    output = await orchestrator.handle(_gate_evaluated())

    assert len(_deploy_publishes([output])) == 1
