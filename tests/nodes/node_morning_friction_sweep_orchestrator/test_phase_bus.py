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

from omnimarket.nodes.node_morning_friction_sweep_orchestrator.handlers.handler_friction_phase_bus import (
    HandlerFrictionPhaseBus,
)
from omnimarket.nodes.node_morning_friction_sweep_orchestrator.handlers.handler_morning_friction_sweep import (
    OVERLAY_ENV,
    HandlerMorningFrictionSweep,
)
from omnimarket.nodes.node_morning_friction_sweep_orchestrator.models import (
    ModelFrictionPhaseRequest,
)

from .helpers import NODE, OVERLAY_FILE, request, stub_result


class Rpc:
    def __init__(self, content: str, tenant: str = "tenant-a") -> None:
        self.calls: list[dict[str, object]] = []
        self.instances: list[str] = []
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
        assert timeout_seconds == 7200
        return {
            "payload": {
                "correlation_id": str(envelope.correlation_id),
                "tenant_id": self.tenant,
                "content": self.content,
            }
        }


def _phase(label: str = "friction-scan") -> ModelFrictionPhaseRequest:
    return ModelFrictionPhaseRequest(
        label=label,
        phase="Scan",
        model="sonnet",
        effort="high",
        prompt="fixture",
        schema_definition={"type": "object"},
    )


def test_phase_gateway_uses_canonical_delegation_rpc() -> None:
    rpc = Rpc(json.dumps(stub_result("friction-scan")))
    result = asyncio.run(HandlerFrictionPhaseBus(rpc=rpc).run(request(), _phase()))
    assert result is not None
    assert result["handoff_path"] == "/tmp/scan.json"
    assert rpc.instances == ["friction_phase"]
    envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate(rpc.calls[0])
    assert envelope.event_type == "onex.cmd.omnibase-infra.delegation-request.v1"
    assert json.loads(envelope.payload.context_pack or "{}") == {
        "phase": "Scan",
        "model": "sonnet",
        "effort": "high",
    }


def test_a_null_phase_result_is_returned_as_absent() -> None:
    assert (
        asyncio.run(HandlerFrictionPhaseBus(rpc=Rpc("null")).run(request(), _phase()))
        is None
    )


def test_a_reply_for_another_tenant_is_refused() -> None:
    gateway = HandlerFrictionPhaseBus(
        rpc=Rpc(json.dumps(stub_result("friction-scan")), tenant="tenant-b")
    )
    with pytest.raises(ValueError, match="tenant does not match"):
        asyncio.run(gateway.run(request(), _phase()))


def test_a_non_object_phase_result_is_refused() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        asyncio.run(HandlerFrictionPhaseBus(rpc=Rpc("[1]")).run(request(), _phase()))


def test_a_phase_result_off_its_typed_schema_is_refused() -> None:
    gateway = HandlerFrictionPhaseBus(rpc=Rpc(json.dumps({"handoff_path": 1})))
    with pytest.raises(ValueError, match="validation error"):
        asyncio.run(gateway.run(request(), _phase()))


def test_a_run_without_the_runtime_bus_is_refused() -> None:
    with pytest.raises(ValueError, match="runtime event_bus"):
        asyncio.run(HandlerFrictionPhaseBus().run(request(), _phase()))


def test_kernel_injects_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    bus = EventBusInmemory(environment="dev", group="friction-proof")
    context = ModelHandlerResolverContext(
        handler_cls=HandlerMorningFrictionSweep,
        handler_module=HandlerMorningFrictionSweep.__module__,
        handler_name="HandlerMorningFrictionSweep",
        contract_name=NODE,
        node_name=NODE,
        event_bus=bus,
    )
    resolution = ServiceHandlerResolver().resolve(context)
    assert isinstance(resolution.handler_instance, HandlerMorningFrictionSweep)
    gateway = resolution.handler_instance.gateway
    assert isinstance(gateway, HandlerFrictionPhaseBus)
    assert cast(object, gateway._event_bus) is bus
