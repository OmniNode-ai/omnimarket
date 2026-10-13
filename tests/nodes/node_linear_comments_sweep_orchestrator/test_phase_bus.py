# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both gateways ride the canonical request/reply wiring, and refuse a reply that is not theirs."""

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

from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    OVERLAY_ENV,
    HandlerLinearCommentsSweep,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_sweep_bus import (
    HandlerSweepBus,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
    ModelSweepPhaseRequest,
)

from .helpers import NODE, OVERLAY_FILE, request


class Rpc:
    def __init__(self, reply: dict[str, object]) -> None:
        self.calls: list[tuple[str, dict[str, object], int | None]] = []
        self.reply = reply

    async def send_request(
        self,
        instance_name: str,
        payload: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        self.calls.append((instance_name, payload, timeout_seconds))
        reply = dict(self.reply)
        inner = reply.get("payload")
        if isinstance(inner, dict) and "correlation_id" not in inner:
            reply["correlation_id"] = payload["correlation_id"]
        elif isinstance(inner, dict) and inner["correlation_id"] == "echo":
            reply["payload"] = {**inner, "correlation_id": payload["correlation_id"]}
        return reply


def _phase(task_type: str = "document") -> ModelSweepPhaseRequest:
    return ModelSweepPhaseRequest.model_validate(
        {
            "label": "delegate:OMN-1:abc",
            "phase": "Draft",
            "task_type": task_type,
            "prompt": "draft it",
            "max_tokens": 2048,
            "timeout_seconds": 300,
            "schema_definition": {"type": "object"},
        }
    )


def _terminal(**fields: object) -> dict[str, object]:
    return {
        "payload": {
            "correlation_id": "echo",
            "tenant_id": "tenant-a",
            "content": '{"x": 1}',
            "model_used": "qwen",
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
            "latency_ms": 321,
            "attempts_count": 2,
            "escalation_count": 1,
            **fields,
        }
    }


def test_a_model_call_uses_the_canonical_delegation_rpc() -> None:
    rpc = Rpc(_terminal())
    reply = asyncio.run(HandlerSweepBus(rpc=rpc).run(request(), _phase()))
    assert reply.ok
    assert reply.content == '{"x": 1}'
    assert (reply.model_used, reply.total_tokens, reply.latency_ms) == ("qwen", 12, 321)
    assert (reply.attempts, reply.escalations) == (2, 1)
    name, payload, timeout = rpc.calls[0]
    assert (name, timeout) == ("sweep_phase", 300)
    envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate(payload)
    assert envelope.event_type == "onex.cmd.omnibase-infra.delegation-request.v1"
    assert envelope.payload.task_type == "document"
    assert envelope.payload.max_tokens == 2048
    # A document call never asks for an execution timeout the task class ceiling may refuse.
    assert envelope.payload.requested_timeout_seconds is None
    assert json.loads(envelope.payload.context_pack or "{}") == {
        "phase": "Draft",
        "model": "",
        "effort": "",
    }


def test_an_agent_call_asks_for_its_execution_timeout() -> None:
    rpc = Rpc(_terminal())
    asyncio.run(HandlerSweepBus(rpc=rpc).run(request(), _phase("agent_delegation")))
    envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate(
        rpc.calls[0][1]
    )
    assert envelope.payload.requested_timeout_seconds == 300


def test_a_failed_terminal_is_returned_with_its_reason_not_raised() -> None:
    rpc = Rpc(_terminal(content="", failure_reason="proven floor not met"))
    reply = asyncio.run(HandlerSweepBus(rpc=rpc).run(request(), _phase()))
    assert not reply.ok
    assert reply.failure == "proven floor not met"


def test_an_empty_answer_with_no_reason_still_says_why() -> None:
    reply = asyncio.run(
        HandlerSweepBus(rpc=Rpc(_terminal(content="  "))).run(request(), _phase())
    )
    assert not reply.ok
    assert "no content" in reply.failure


def test_a_reply_for_another_tenant_is_refused() -> None:
    with pytest.raises(ValueError, match="tenant does not match"):
        asyncio.run(
            HandlerSweepBus(rpc=Rpc(_terminal(tenant_id="tenant-b"))).run(
                request(), _phase()
            )
        )


def test_a_reply_for_another_correlation_is_refused() -> None:
    with pytest.raises(ValueError, match="correlation does not match"):
        asyncio.run(
            HandlerSweepBus(rpc=Rpc(_terminal(correlation_id="other"))).run(
                request(), _phase()
            )
        )


def test_a_decision_rides_the_decision_nodes_command_topic() -> None:
    rpc = Rpc({"payload": {"kind": "tally", "degraded": False}})
    answer = asyncio.run(
        HandlerSweepBus(rpc=rpc).decide(
            request(),
            {"kind": "tally", "held": [], "needing_count": 0, "accepted_count": 0},
        )
    )
    assert answer == {"kind": "tally", "degraded": False}
    name, payload, timeout = rpc.calls[0]
    assert (name, timeout) == ("sweep_decision", 60)
    assert payload["event_type"] == (
        "onex.cmd.omnimarket.linear-comments-decision-requested.v1"
    )
    assert cast(dict[str, object], payload["payload"])["kind"] == "tally"


def test_a_decision_answering_another_kind_is_refused() -> None:
    rpc = Rpc({"payload": {"kind": "metrics_row"}})
    with pytest.raises(ValueError, match="the answer is for metrics_row"):
        asyncio.run(
            HandlerSweepBus(rpc=rpc).decide(
                request(),
                {"kind": "tally", "held": [], "needing_count": 0, "accepted_count": 0},
            )
        )


def test_each_decision_gets_its_own_correlation() -> None:
    rpc = Rpc({"payload": {"kind": "tally"}})
    bus = HandlerSweepBus(rpc=rpc)
    ask: dict[str, JsonValue] = {
        "kind": "tally",
        "held": [],
        "needing_count": 0,
        "accepted_count": 0,
    }
    asyncio.run(bus.decide(request(), ask))
    asyncio.run(bus.decide(request(), ask))
    assert rpc.calls[0][1]["correlation_id"] != rpc.calls[1][1]["correlation_id"]


def test_a_run_without_the_runtime_bus_is_refused() -> None:
    with pytest.raises(ValueError, match="runtime event_bus"):
        asyncio.run(HandlerSweepBus().run(request(), _phase()))


def test_kernel_injects_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    bus = EventBusInmemory(environment="dev", group="sweep-proof")
    context = ModelHandlerResolverContext(
        handler_cls=HandlerLinearCommentsSweep,
        handler_module=HandlerLinearCommentsSweep.__module__,
        handler_name="HandlerLinearCommentsSweep",
        contract_name=NODE,
        node_name=NODE,
        event_bus=bus,
    )
    resolution = ServiceHandlerResolver().resolve(context)
    assert isinstance(resolution.handler_instance, HandlerLinearCommentsSweep)
    gateway = resolution.handler_instance.gateway
    assert isinstance(gateway, HandlerSweepBus)
    assert resolution.handler_instance.decisions is gateway
    assert cast(object, gateway._event_bus) is bus
