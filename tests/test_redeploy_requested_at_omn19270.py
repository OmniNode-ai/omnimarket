# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A redeploy carries when it was requested, all the way to the deploy agent (OMN-19270).

The deploy agent's lineage fence supersedes a sibling-triggered rebuild when the
lane's running workspace build started after the rebuild was requested, because
that build staged the sibling from a dev branch that already held the merge.
The agent can only compare with a time it is given. The CI trigger publishes
the start command with no time, and the command then waits behind every deploy
ahead of it, so the broker timestamp the agent sees is the forwarding hop's,
milliseconds before it reads the record.

These tests pin the carry: the orchestrator stamps its receipt when the request
has no time, keeps one it was given, carries it across the gate hop, and the
publish monitor puts it on the wire command. An unknown time is omitted from
the wire, never sent as null.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumRedeployScope,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelProdPromotionGateDecision,
    ModelRedeployCommand,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers import (
    handler_redeploy_orchestrator as module,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type

pytestmark = pytest.mark.unit

_PUBLISHED = datetime(2026, 9, 23, 13, 52, 44, tzinfo=UTC)


def _start(requested_at: datetime | None = None) -> ModelRedeployStartCommand:
    return ModelRedeployStartCommand(
        correlation_id=uuid4(),
        scope=EnumRedeployScope.FULL,
        git_ref="0edf5c9145876dbe22f6caf7a06b507c5d0fc7d3",
        runtime_lane=EnumRuntimeLane.DEV,
        build_source=EnumBuildSource.WORKSPACE,
        requested_by="gha/omnimarket/pr-2808",
        requested_at=requested_at,
    )


def _through_the_gate(start: ModelRedeployStartCommand) -> ModelDeployPublishCommand:
    """Run the start command through ``_on_start`` and the gate hop to the publish."""
    handler = module.HandlerRedeployOrchestrator()
    [gate] = handler._on_start(
        ModelEventEnvelope(
            payload=start, correlation_id=start.correlation_id, event_type="start"
        ),
        start.correlation_id,
    )
    decision = ModelProdPromotionGateDecision.model_validate(
        {
            "allowed": True,
            "reason": "non-prod lane",
            "deploy_context": gate.payload.deploy_context.model_dump(mode="json"),
        }
    )
    [published] = handler._on_gate_evaluated(
        ModelEventEnvelope(
            payload=decision,
            correlation_id=start.correlation_id,
            event_type=publisher_event_type(
                "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1"
            ),
        ),
        start.correlation_id,
    )
    assert isinstance(published.payload, ModelDeployPublishCommand)
    return published.payload


def test_the_orchestrator_stamps_its_receipt_when_the_request_has_no_time() -> None:
    before = datetime.now(UTC)

    published = _through_the_gate(_start())

    assert published.requested_at is not None
    assert before <= published.requested_at <= datetime.now(UTC)


def test_a_request_that_carries_its_time_keeps_it_across_the_gate() -> None:
    assert _through_the_gate(_start(_PUBLISHED)).requested_at == _PUBLISHED


def test_the_shared_command_s_own_request_time_is_carried() -> None:
    correlation_id = uuid4()
    shared = ModelRedeployCommand(
        correlation_id=correlation_id,
        requested_at=_PUBLISHED,
        runtime_lane=EnumRuntimeLane.PROD,
        image_digest="sha256:" + "b" * 64,
        promotion_batch_id="batch-omn19270",
    )

    assert module._coerce_start(shared, correlation_id).requested_at == _PUBLISHED


async def _wire_command(requested_at: datetime | None) -> dict[str, object]:
    bus = EventBusInmemory(environment="test", group="requested-at-test")
    await bus.start()
    seen: list[dict[str, object]] = []

    async def _agent(message: object) -> None:
        seen.append(json.loads(message.value))  # type: ignore[attr-defined]

    await bus.subscribe(TOPIC_REBUILD_REQUESTED, on_message=_agent, group_id="agent")
    handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=0.1)
    await handler.publish_and_monitor(
        ModelDeployPublishCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            requested_at=requested_at,
        )
    )
    await bus.close()
    assert len(seen) == 1
    return seen[0]


async def test_the_wire_command_carries_the_request_time() -> None:
    wire = await _wire_command(_PUBLISHED)

    assert datetime.fromisoformat(str(wire["requested_at"])) == _PUBLISHED


async def test_an_unknown_request_time_is_omitted_from_the_wire() -> None:
    wire = await _wire_command(None)

    assert "requested_at" not in wire
