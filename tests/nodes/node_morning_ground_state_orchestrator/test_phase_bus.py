# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The phase gateway rides the canonical delegation request/reply wiring."""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.resolver.model_handler_resolver_context import (
    ModelHandlerResolverContext,
)
from omnibase_core.services.service_handler_resolver import ServiceHandlerResolver
from pydantic import JsonValue

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    OVERLAY_ENV,
    HandlerMorningGroundState,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_phase_bus import (
    HandlerMorningPhaseBus,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
    ModelMorningPhaseRequest,
)

from .helpers import OVERLAY_FILE, public_overlay, request

STUB = {
    "state": "stub",
    "detail": "fixture",
    "prs": [],
    "residuals": [],
    "delegation": {"delegated": 1, "runs": ["fixture"], "reason": ""},
}


class Rpc:
    def __init__(self, content: str, tenant: str = "tenant-a") -> None:
        self.calls: list[dict[str, object]] = []
        self.instances: list[str] = []
        self.prompt = ""
        self.content = content
        self.tenant = tenant

    async def send_request(
        self,
        instance_name: str,
        payload: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        self.calls.append(payload)
        self.instances.append(instance_name)
        envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate(payload)
        assert envelope.payload.task_type == "agent_delegation"
        assert envelope.payload.tenant_id == "tenant-a"
        assert timeout_seconds == 14400
        self.prompt = envelope.payload.prompt
        return {
            "payload": {
                "correlation_id": str(envelope.correlation_id),
                "tenant_id": self.tenant,
                "content": self.content,
            }
        }


def _phase(label: str = "ground-state") -> ModelMorningPhaseRequest:
    return ModelMorningPhaseRequest(
        label=label,
        phase="GroundState",
        model="opus",
        effort="high",
        prompt="fixture",
        schema_definition={"type": "object"},
    )


def test_phase_gateway_uses_canonical_delegation_rpc() -> None:
    rpc = Rpc(json.dumps(STUB))
    gateway = HandlerMorningPhaseBus(rpc=rpc, overlay=public_overlay())
    result = asyncio.run(gateway.run(request(), _phase()))
    assert result is not None
    assert result["state"] == "stub"
    assert rpc.instances == ["morning_phase"]
    envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate(rpc.calls[0])
    assert envelope.event_type == "onex.cmd.omnibase-infra.delegation-request.v1"


def test_a_reply_for_another_tenant_is_refused() -> None:
    gateway = HandlerMorningPhaseBus(
        rpc=Rpc(json.dumps(STUB), tenant="tenant-b"), overlay=public_overlay()
    )
    with pytest.raises(ValueError, match="tenant does not match"):
        asyncio.run(gateway.run(request(), _phase()))


def test_a_non_object_phase_result_is_refused() -> None:
    gateway = HandlerMorningPhaseBus(rpc=Rpc("[1]"), overlay=public_overlay())
    with pytest.raises(ValueError, match="must be an object"):
        asyncio.run(gateway.run(request(), _phase("ledger-reconcile")))


def test_a_phase_result_off_its_typed_schema_is_refused() -> None:
    gateway = HandlerMorningPhaseBus(
        rpc=Rpc(json.dumps({"state": 1})), overlay=public_overlay()
    )
    with pytest.raises(ValueError, match="validation error"):
        asyncio.run(gateway.run(request(), _phase()))


def test_a_run_without_the_runtime_bus_is_refused() -> None:
    with pytest.raises(ValueError, match="runtime event_bus"):
        asyncio.run(
            HandlerMorningPhaseBus(overlay=public_overlay()).run(request(), _phase())
        )


def test_kernel_injects_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    bus = EventBusInmemory(environment="dev", group="morning-proof")
    context = ModelHandlerResolverContext(
        handler_cls=HandlerMorningGroundState,
        handler_module=HandlerMorningGroundState.__module__,
        handler_name="HandlerMorningGroundState",
        contract_name="node_morning_ground_state_orchestrator",
        node_name="node_morning_ground_state_orchestrator",
        event_bus=bus,
    )
    resolution = ServiceHandlerResolver().resolve(context)
    assert isinstance(resolution.handler_instance, HandlerMorningGroundState)
    gateway = resolution.handler_instance.gateway
    assert isinstance(gateway, HandlerMorningPhaseBus)
    assert cast(object, gateway._event_bus) is bus


@pytest.mark.parametrize("live_lanes", [None, [], ["running-lane"]])
def test_ledger_reconcile_requires_a_supplied_roster_for_silent_retirement(
    live_lanes: list[str] | None,
) -> None:
    rpc = Rpc("{}")
    args: dict[str, JsonValue] = {}
    if live_lanes is not None:
        args["liveLanes"] = cast(JsonValue, live_lanes)
    run = request(args)
    assert run.live_lanes == live_lanes
    asyncio.run(
        HandlerMorningPhaseBus(rpc=rpc, overlay=public_overlay()).reconcile(run)
    )
    assert "ledger-reconcile-tool --apply" in rpc.prompt
    assert ("--live-roster" in rpc.prompt) is (live_lanes is not None)
    assert ("--silent-hours 72" in rpc.prompt) is (live_lanes is not None)
    if live_lanes is not None:
        for lane in live_lanes:
            assert f"--live-lane {lane}" in rpc.prompt
    assert "--stale-hours 6 --since-days 0 --max-appends 60 --json" in rpc.prompt


def test_an_invalid_live_lane_name_skips_reconciliation() -> None:
    rpc = Rpc("{}")
    asyncio.run(
        HandlerMorningPhaseBus(rpc=rpc, overlay=public_overlay()).reconcile(
            request({"liveLanes": ["bad lane; rm"]})
        )
    )
    assert rpc.calls == []
