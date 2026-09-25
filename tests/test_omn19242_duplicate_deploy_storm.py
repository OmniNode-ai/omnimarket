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
    Since OMN-18143 the command arm does not wait at all: it publishes and returns,
    and the rejection is read by the effect's durable rejection arm. The AC1 tests
    below pin that stronger form: the dispatch is over before the agent answers, a
    rejection rolls nothing back, and a peer's rejection touches nothing of this one.

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
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_REJECTED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    HandlerRedeployOrchestrator,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type
from tests.test_omn19377_deploy_effect_skips_repeats import _CountingBus, _DurableArms

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
async def test_ac1_the_dispatch_is_over_before_the_agent_rejects() -> None:
    """The live sequence: publish, agent rejects as duplicate one second later."""
    bus = EventBusInmemory(environment="test", group="omn19242")
    await bus.start()
    try:
        arms = await _DurableArms(bus).start()
        await _rejecting_agent(
            bus, reason=EnumDeployRejectionReason.DUPLICATE, delay_s=1.0
        )
        handler = HandlerDeployPublishMonitor(event_bus=bus)

        output = await handler.handle(_command_envelope(_LIVE_CORRELATION))
        answered_before_return = list(arms.outputs)
        (rejection,) = await arms.wait_for(1, timeout_s=5.0)
    finally:
        await bus.close()

    # Causal, not a stopwatch: the dispatch was over before the agent's answer existed.
    assert answered_before_return == [], "the dispatch waited for the agent's answer"
    assert output.metrics["rebuild_published"] == 1.0
    # A rejected command never went live, so there is nothing to roll back.
    assert rejection.metrics["rebuild_rejected_observed"] == 1.0
    assert rejection.events == ()


@pytest.mark.unit
async def test_ac1_a_busy_for_another_correlation_releases_nothing_here() -> None:
    """Only this command's own answer counts: a peer's busy must not release it."""
    bus = _CountingBus()
    await bus.start()
    try:
        arms = await _DurableArms(bus).start()
        await _rejecting_agent(
            bus,
            reason=EnumDeployRejectionReason.BUSY,
            delay_s=0.05,
            correlation_override=str(uuid4()),
        )
        mine = str(uuid4())
        handler = HandlerDeployPublishMonitor(event_bus=bus)
        await handler.handle(_command_envelope(mine))
        await arms.wait_for(1)
        repeat = await handler.handle(_command_envelope(mine))
    finally:
        await bus.close()

    assert bus.commands == [mine]
    assert repeat.metrics["duplicate_skipped"] == 1.0


@pytest.mark.unit
async def test_ac1_the_command_arm_reports_the_rejection_and_rolls_nothing_back() -> (
    None
):
    """Through ``handle``, the entry the runtime calls: fast, and no rollback event."""
    bus = EventBusInmemory(environment="test", group="omn19242")
    await bus.start()
    try:
        arms = await _DurableArms(bus).start()
        await _rejecting_agent(
            bus, reason=EnumDeployRejectionReason.DUPLICATE, delay_s=0.05
        )
        started = time.monotonic()
        output = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _command_envelope(_LIVE_CORRELATION)
        )
        elapsed = time.monotonic() - started
        outputs = await arms.wait_for(1)
    finally:
        await bus.close()

    assert elapsed < 5.0
    assert output.events == ()
    assert [o.events for o in outputs] == [()]
    assert all(o.metrics.get("rolled_back", 0.0) == 0.0 for o in outputs)


def _command_envelope(correlation_id: str) -> ModelEventEnvelope[Any]:
    command = ModelDeployPublishCommand(
        correlation_id=UUID(correlation_id), runtime_lane=EnumRuntimeLane.DEV
    )
    return ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
    )


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
