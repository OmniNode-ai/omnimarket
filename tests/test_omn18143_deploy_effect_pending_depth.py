# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy effect reports how many published rebuild commands are still pending (OMN-18143 AC2).

WHAT WAS OBSERVED ON THE DEV LANE, 2026-09-10
    A stalled channel could be seen only by reading the broker topic by hand: the
    GitHub side saw ``published: true`` and the agent side saw an empty queue, and
    neither could see the effect's own backlog.

WHAT THIS PINS
    The effect no longer holds a command while it waits for the agent (AC3), so its
    backlog is the set of commands it has published that no completion or terminal
    rejection has answered yet. Every arm reports that depth for the lane as the
    ``pending_depth`` metric, so a depth that only grows is a stall that can be read
    off the arms' output instead of the topic.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumDeployRejectionReason,
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_COMPLETED,
    TOPIC_REBUILD_REJECTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type
from tests.test_omn19377_deploy_effect_skips_repeats import _CountingBus


def _command(
    correlation_id: UUID, lane: EnumRuntimeLane = EnumRuntimeLane.DEV
) -> ModelEventEnvelope[Any]:
    command = ModelDeployPublishCommand(
        correlation_id=correlation_id, runtime_lane=lane
    )
    return ModelEventEnvelope(
        payload=command,
        correlation_id=correlation_id,
        event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
    )


def _completed(correlation_id: UUID, lane: EnumRuntimeLane) -> ModelEventEnvelope[Any]:
    return ModelEventEnvelope(
        payload=ModelDeployRebuildCompleted(
            correlation_id=str(correlation_id),
            status=EnumRedeployStatus.SUCCESS,
            runtime_lane=lane,
        ).model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type=TOPIC_REBUILD_COMPLETED,
    )


def _rejected(
    correlation_id: UUID, reason: EnumDeployRejectionReason
) -> ModelEventEnvelope[Any]:
    replacement: dict[str, Any] = (
        {"superseded_by_sha": "a" * 40, "superseded_by_correlation_id": uuid4()}
        if reason is EnumDeployRejectionReason.SUPERSEDED
        else {}
    )
    return ModelEventEnvelope(
        payload=ModelDeployRebuildRejected(
            correlation_id=correlation_id, reason=reason, scope="full", **replacement
        ).model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type=TOPIC_REBUILD_REJECTED,
    )


async def _handle(bus: _CountingBus, envelope: ModelEventEnvelope[Any]) -> Any:
    """Each message goes to a fresh handler, as the runtime builds one per routing entry."""
    return await HandlerDeployPublishMonitor(event_bus=bus).handle(envelope)


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))


@pytest.mark.unit
async def test_the_command_arm_reports_the_commands_it_has_published_and_nobody_answered() -> (
    None
):
    bus = _CountingBus()
    await bus.start()
    try:
        depths = [
            (await _handle(bus, _command(uuid4()))).metrics["pending_depth"]
            for _ in range(3)
        ]
    finally:
        await bus.close()

    assert depths == [1.0, 2.0, 3.0]


@pytest.mark.unit
async def test_depth_is_counted_per_lane() -> None:
    bus = _CountingBus()
    await bus.start()
    try:
        await _handle(bus, _command(uuid4(), EnumRuntimeLane.DEV))
        await _handle(bus, _command(uuid4(), EnumRuntimeLane.DEV))
        stability = await _handle(
            bus, _command(uuid4(), EnumRuntimeLane.STABILITY_TEST)
        )
        dev = await _handle(bus, _command(uuid4(), EnumRuntimeLane.DEV))
    finally:
        await bus.close()

    assert stability.metrics["pending_depth"] == 1.0
    assert dev.metrics["pending_depth"] == 3.0


@pytest.mark.unit
async def test_a_completion_takes_its_command_out_of_the_depth() -> None:
    bus = _CountingBus()
    first, second = uuid4(), uuid4()
    await bus.start()
    try:
        await _handle(bus, _command(first))
        await _handle(bus, _command(second))
        completion = await _handle(bus, _completed(first, EnumRuntimeLane.DEV))
        redelivered = await _handle(bus, _completed(first, EnumRuntimeLane.DEV))
        third = await _handle(bus, _command(uuid4()))
    finally:
        await bus.close()

    assert completion.metrics["pending_depth"] == 1.0
    assert redelivered.metrics["pending_depth"] == 1.0
    assert third.metrics["pending_depth"] == 2.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason",
    [
        EnumDeployRejectionReason.SUPERSEDED,
        EnumDeployRejectionReason.SUPERSEDED_BY_RUNNING_BUILD,
        EnumDeployRejectionReason.DIVERGENT_REF,
        EnumDeployRejectionReason.LANE_NOT_ALLOWED,
        EnumDeployRejectionReason.INVALID_SIGNATURE,
        EnumDeployRejectionReason.IN_PROGRESS,
        EnumDeployRejectionReason.BUSY,
    ],
)
async def test_a_command_the_agent_will_not_run_leaves_the_depth(
    reason: EnumDeployRejectionReason,
) -> None:
    """No completion ever arrives for these, so counting them would grow the depth forever."""
    bus = _CountingBus()
    dropped = uuid4()
    await bus.start()
    try:
        await _handle(bus, _command(dropped))
        await _handle(bus, _command(uuid4()))
        rejection = await _handle(bus, _rejected(dropped, reason))
        next_command = await _handle(bus, _command(uuid4()))
    finally:
        await bus.close()

    assert rejection.metrics["pending_depth"] == 1.0
    assert next_command.metrics["pending_depth"] == 2.0


@pytest.mark.unit
async def test_a_refusal_of_a_copy_leaves_the_original_pending() -> None:
    """The agent still holds the original of a duplicate; its completion is still coming."""
    reason = EnumDeployRejectionReason.DUPLICATE
    bus = _CountingBus()
    original = uuid4()
    await bus.start()
    try:
        await _handle(bus, _command(original))
        rejection = await _handle(bus, _rejected(original, reason))
        next_command = await _handle(bus, _command(uuid4()))
    finally:
        await bus.close()

    assert rejection.metrics["pending_depth"] == 1.0
    assert next_command.metrics["pending_depth"] == 2.0


@pytest.mark.unit
async def test_a_repeat_skipped_by_the_effect_does_not_deepen_the_backlog() -> None:
    bus = _CountingBus()
    correlation = uuid4()
    await bus.start()
    try:
        first = await _handle(bus, _command(correlation))
        repeat = await _handle(bus, _command(correlation))
    finally:
        await bus.close()

    assert first.metrics["pending_depth"] == 1.0
    assert repeat.metrics["pending_depth"] == 1.0
